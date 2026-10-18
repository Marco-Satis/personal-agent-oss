"""Command line: serve, escalate, status, simulate, chat."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import site
import sys
import sysconfig
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

import personal_agent

from . import __version__
from .config import Config, ConfigError, validate_pin
from .conversation import Call
from .escalation import SEVERITIES, Event
from .status import clean_text, format_status_line, read_statuses


def _cmd_serve(config: Config, _args: argparse.Namespace) -> int:
    from .server import serve

    serve(config)
    return 0


def _local_url(config: Config, path: str) -> str:
    """URL of our own server. A wildcard bind address is reached through loopback."""
    host = {"0.0.0.0": "127.0.0.1", "::": "::1", "": "127.0.0.1"}.get(config.bind_host, config.bind_host)
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{config.port}{path}"


def _cmd_escalate(config: Config, args: argparse.Namespace) -> int:
    from .remote import local_opener

    if not config.api_token:
        print("PA_API_TOKEN is not set", file=sys.stderr)
        return 2
    payload = json.dumps(
        {"key": args.key, "severity": args.severity, "summary": args.summary, "source": args.source}
    ).encode()
    request = urllib.request.Request(_local_url(config, "/escalate"), data=payload, method="POST")
    request.add_unredirected_header("Authorization", f"Bearer {config.api_token}")
    request.add_header("Content-Type", "application/json")
    try:
        with local_opener()(request, timeout=20) as response:
            print(response.read().decode())
    except urllib.error.HTTPError as exc:
        print(f"the agent answered HTTP {exc.code}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"could not reach the agent: {exc.reason}", file=sys.stderr)
        return 1
    return 0


def _cmd_status(config: Config, _args: argparse.Namespace) -> int:
    statuses = read_statuses(config.status_dir, config.status_max_age_min)
    if not statuses:
        print(f"no session status in {config.status_dir}")
    for s in statuses:
        print(format_status_line(s))
    return 0


def _cmd_simulate(config: Config, args: argparse.Namespace) -> int:
    """Play a call in the terminal. Type digits (e.g. '123456#', '1') or words."""
    pin = config.call_pin or "000000"
    validate_pin(pin)
    event = Event(key="simulated", severity="critical", summary=args.summary, source="simulator")
    call = Call(
        pin=pin,
        event_key=event.key,
        event_summary=event.summary,
        event_source=event.source,
        read_statuses=lambda: read_statuses(config.status_dir, config.status_max_age_min),
        acknowledge=lambda key: print(f"[acknowledged {key}]"),
        language=config.language,
    )
    if not config.call_pin:
        print("[PA_CALL_PIN not set, simulator PIN is 000000]")
    turn = call.start()
    while True:
        for line in turn.say:
            print(f"AGENT: {line}")
        if turn.expect is None:
            print("[call ended]")
            return 0
        try:
            entered = input("YOU (digits or words, empty = silence): ").strip()
        except EOFError:
            return 0
        if entered and entered.rstrip("#").isdigit():
            turn = call.handle(digits=entered)
        else:
            turn = call.handle(speech=entered or None)


CHAT_TOOLS = ("Read", "Glob", "Grep", "Write", "Edit")


def _inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _python_paths() -> list[Path]:
    """Where this interpreter loads code from at startup (prefixes, stdlib, site-packages)."""
    raw: list[str] = [sys.executable, os.path.dirname(sys.executable), sys.prefix, sys.base_prefix,
                      sys.exec_prefix, sys.base_exec_prefix]
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        raw.append(sysconfig.get_paths().get(key) or "")
    try:
        raw += site.getsitepackages()
        raw.append(site.getusersitepackages())
    except (AttributeError, OSError):  # some embedded or venv builds lack these
        pass
    return [Path(p) for p in raw if p]


def _workdir_problem(config: Config, workdir: Path, config_dir: Path) -> str | None:
    """Reasons the assistant's working directory is unsafe to use.

    The assistant may write inside the workdir (after approval). Nothing that runs
    code or that the agent trusts may live there: the package, the interpreter and
    its site-packages (.pth files run at startup), the Claude Code binary and
    config, the state, the session status files (they can trigger phone calls).
    """
    if not os.path.isabs(sys.executable or ""):
        return "the Python interpreter path is not absolute"
    root = workdir.resolve()
    package = Path(personal_agent.__file__).resolve().parent
    if _inside(package, root) or _inside(root, package):
        return "workdir must not contain the personal_agent package"
    if root == Path("/") or root == Path.home().resolve():
        return "workdir must not be / or the home directory"
    if _inside(root, config.status_dir.resolve()):
        return "workdir must not be inside the session status directory"
    claude_bin = shutil.which(config.claude_bin)
    checks: list[tuple[str, Path]] = [
        ("the Claude Code config directory", config_dir),
        ("the state directory", config.state_dir),
        ("the session status directory", config.status_dir),
        ("the system temp directory", Path(tempfile.gettempdir())),
    ]
    checks += [("the Python interpreter or its packages", p) for p in _python_paths()]
    if claude_bin:
        checks.append(("the Claude Code binary", Path(claude_bin)))
    for name, path in checks:
        for candidate in {path, path.resolve()}:  # a venv's python is often a symlink out
            if _inside(candidate.absolute(), root):
                return f"workdir must not contain {name}"
    return None


def _cmd_chat(config: Config, args: argparse.Namespace) -> int:
    """Talk to the assistant in the terminal. Writing asks for your approval first."""
    from .approval import PERMISSION_TOOL, ApprovalBroker, ConsoleChannel
    from .bridge import DEFAULT_TURN_TIMEOUT_S, BridgeConfig, BridgeError, ClaudeSession
    from .quota import QuotaGuard
    from .remote import RemoteChannel, local_opener
    from .router import Router, RouterConfig

    remote = bool(getattr(args, "remote_approvals", False))
    if remote and not config.api_token:
        print("[error] --remote-approvals needs PA_API_TOKEN (the same as for serve)", file=sys.stderr)
        return 2
    workdir, config_dir = config.bridge_paths()
    problem = _workdir_problem(config, workdir, config_dir)
    if problem:
        print(f"[error] {problem}", file=sys.stderr)
        return 2
    root = workdir.resolve()

    console_lock = threading.Lock()
    broker = ApprovalBroker(workdir, config.approval_timeout_s)
    def stop() -> bool:
        return broker.closing.is_set() or not broker.turn_active.is_set()

    if remote:
        assert config.api_token
        try:
            with local_opener()(_local_url(config, "/healthz"), timeout=3) as response:
                response.read(64)
        except (OSError, ValueError):
            print("[warning] `personal-agent serve` is not reachable: remote approvals will be denied")
        broker.channel = RemoteChannel(_local_url(config, ""), config.api_token, console_lock, stop=stop)
    else:
        broker.channel = ConsoleChannel(console_lock, stop=stop)
    runtime_parent: Path | None = config.state_dir / "run"
    if _inside(runtime_parent.resolve(), root):
        runtime_parent = None
    try:
        mcp_path = broker.start(runtime_parent)
    except OSError as exc:
        print(f"[error] could not start the approval service: {type(exc).__name__}", file=sys.stderr)
        return 1
    if broker.runtime_dir is None or _inside(broker.runtime_dir.resolve(), root):
        broker.close()
        print("[error] the approval runtime directory must not be inside the workdir", file=sys.stderr)
        return 2

    main_session = ClaudeSession(
        BridgeConfig(
            workdir=workdir,
            config_dir=config_dir,
            claude_bin=config.claude_bin,
            model=args.model or config.claude_model,
            tools=CHAT_TOOLS,
            mcp_config=mcp_path,
            permission_tool=PERMISSION_TOOL,
            oauth_token=config.claude_oauth_token,
        ),
        extra_env={"MCP_TOOL_TIMEOUT": str((config.approval_timeout_s + 30) * 1000)},
        turn_timeout_s=max(DEFAULT_TURN_TIMEOUT_S, config.approval_timeout_s + 120),
    )
    # Tier 2: no tools at all, no MCP servers, no permission tool. Starts on first use.
    small_session = None
    if config.router_small_model:
        small_session = ClaudeSession(
            BridgeConfig(
                workdir=workdir,
                config_dir=config_dir,
                claude_bin=config.claude_bin,
                model=config.router_small_model,
                tools=(),
                oauth_token=config.claude_oauth_token,
            )
        )
    router = Router(
        main_session,
        small_session,
        QuotaGuard(config.quota_stop, config.state_dir / "quota.json"),
        RouterConfig(config.router_small_model, config.router_small_max_chars),
        statuses=lambda: read_statuses(config.status_dir, config.status_max_age_min),
    )

    def say(line: str) -> None:
        with console_lock:
            print(line)

    def on_event(event: dict) -> None:
        broker.observe(event)  # first, and without the console lock: approvals wait for it
        if event.get("type") != "assistant":
            return
        message = event.get("message")
        for block in (message.get("content") if isinstance(message, dict) else None) or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                # Never block the event stream on a prompt that holds the console.
                if console_lock.acquire(blocking=False):
                    try:
                        print(f"  [tool: {clean_text(block.get('name'), 80)}]")
                    finally:
                        console_lock.release()

    where = "on your phone" if remote else "here"
    say(f"[workdir {workdir} · writing asks first ({where}) · help for commands · /exit to quit]")
    try:
        while True:
            try:
                text = input("you> ").strip()
            except EOFError:
                break
            if text in ("/exit", "/quit"):
                break
            if not text:
                continue
            broker.begin_turn()
            error: BridgeError | None = None
            try:
                reply = router.handle(text, on_event=on_event)
            except BridgeError as exc:
                error = exc
            finally:
                # End the turn before printing anything: an orphaned approval prompt for a
                # dead Claude Code process holds the console lock until the turn ends.
                broker.end_turn()
            if error is not None:
                say(f"[error] {error}")
                continue
            if reply.refused:
                say(f"[quota] {reply.text}")
                continue
            say(f"agent[{reply.tier}]> {reply.text}")
            result = reply.result
            if result is None:
                continue
            for denial in result.denials:
                name = denial.get("tool_name") if isinstance(denial, dict) else None
                say(f"[not approved: {clean_text(name or 'unknown tool', 80)}]")
            if result.is_error:
                say(f"[turn ended with an error ({result.subtype})]")
                if "logged in" in result.text.lower():
                    say(
                        f"[the assistant uses its own Claude Code config in {config_dir}: "
                        "run `claude setup-token` once and set CLAUDE_CODE_OAUTH_TOKEN, "
                        "or point PA_CLAUDE_CONFIG_DIR at an existing login]"
                    )
    except KeyboardInterrupt:
        return 130
    finally:
        # No console lock here: a prompt thread may hold it; closing the broker ends that prompt.
        router.close()
        broker.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="personal-agent", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="run the HTTP server and the session watch")

    esc = sub.add_parser("escalate", help="send an event to the running server")
    esc.add_argument("key", help="stable id, e.g. 'backup-failed'")
    esc.add_argument("summary", help="one sentence that will be read to you")
    esc.add_argument("--severity", choices=SEVERITIES, default="critical")
    esc.add_argument("--source", default="cli")

    sub.add_parser("status", help="print the session status the agent can see")

    sim = sub.add_parser("simulate", help="play a phone call in the terminal (no provider needed)")
    sim.add_argument("--summary", default="This is a test alert.")

    chat = sub.add_parser("chat", help="talk to the assistant (Claude Code, locked down)")
    chat.add_argument("--model", default=None, help="model alias, default PA_CLAUDE_MODEL or sonnet")
    chat.add_argument(
        "--remote-approvals",
        action="store_true",
        help="ask for approvals through a Discord notice and a one-time link (needs `serve` running)",
    )
    return parser


COMMANDS = {
    "serve": _cmd_serve,
    "escalate": _cmd_escalate,
    "status": _cmd_status,
    "simulate": _cmd_simulate,
    "chat": _cmd_chat,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = Config.from_env()
        return COMMANDS[args.command](config, args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
