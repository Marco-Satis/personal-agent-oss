"""Command line: serve, escalate, status, simulate."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.error
import urllib.request

from . import __version__
from .config import Config, ConfigError, validate_pin
from .conversation import Call
from .escalation import SEVERITIES, Event
from .status import read_statuses


def _cmd_serve(config: Config, _args: argparse.Namespace) -> int:
    from .server import serve

    serve(config)
    return 0


def _cmd_escalate(config: Config, args: argparse.Namespace) -> int:
    if not config.api_token:
        print("PA_API_TOKEN is not set", file=sys.stderr)
        return 2
    payload = json.dumps(
        {"key": args.key, "severity": args.severity, "summary": args.summary, "source": args.source}
    ).encode()
    request = urllib.request.Request(
        f"http://{config.bind_host}:{config.port}/escalate", data=payload, method="POST"
    )
    request.add_header("Authorization", f"Bearer {config.api_token}")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 (local URL)
            print(response.read().decode())
    except urllib.error.URLError as exc:
        print(f"could not reach the agent: {exc.reason}", file=sys.stderr)
        return 1
    return 0


def _cmd_status(config: Config, _args: argparse.Namespace) -> int:
    statuses = read_statuses(config.status_dir, config.status_max_age_min)
    if not statuses:
        print(f"no session status in {config.status_dir}")
    for s in statuses:
        print(f"{s.state:8} {s.age_minutes():4} min  {s.project:20} {s.summary}")
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


def _cmd_chat(config: Config, args: argparse.Namespace) -> int:
    """Talk to the assistant in the terminal. Read-only tools until approvals exist."""
    from .bridge import BridgeConfig, BridgeError, ClaudeSession

    workdir, config_dir = config.bridge_paths()
    session = ClaudeSession(
        BridgeConfig(
            workdir=workdir,
            config_dir=config_dir,
            claude_bin=config.claude_bin,
            model=args.model or config.claude_model,
            oauth_token=config.claude_oauth_token,
        )
    )

    def show(event: dict) -> None:
        if event.get("type") != "assistant":
            return
        for block in (event.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                print(f"  [tool: {block.get('name')}]")

    print(f"[workdir {workdir} · /exit to quit]")
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
            try:
                result = session.send(text, on_event=show)
            except BridgeError as exc:
                print(f"[error] {exc}")
                continue
            print(f"agent> {result.text}")
            if result.is_error:
                print(f"[turn ended with an error ({result.subtype})]")
                if "logged in" in result.text.lower():
                    print(
                        f"[the assistant uses its own Claude Code config in {config_dir}: "
                        "run `claude setup-token` once and set CLAUDE_CODE_OAUTH_TOKEN, "
                        "or point PA_CLAUDE_CONFIG_DIR at an existing login]"
                    )
    finally:
        session.close()
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
