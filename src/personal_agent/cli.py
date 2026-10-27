"""Command line: serve, escalate, status, simulate, chat, vault, plugins, brief, hooks, telegram, safety."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import personal_agent

from . import __version__
from .config import Config, ConfigError, validate_pin
from .conversation import Call
from .escalation import SEVERITIES, Event
from .status import clean_text, format_status_line, read_statuses, session_overview


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


# Moved to assistant.py (shared with serve's Telegram assistant); the names stay importable here.
from .assistant import CHAT_TOOLS, DELEGATION_PROMPT, STATE_ENTRIES  # noqa: E402,F401
from .assistant import _inside, _python_paths  # noqa: E402,F401
from .assistant import workdir_problem as _workdir_problem  # noqa: E402


def _cmd_vault(config: Config, args: argparse.Namespace) -> int:
    """Create the vault skeleton (never overwriting a file) or print its path."""
    from .vault import Vault, VaultError

    path = config.vault_path
    if args.vault_command == "path":
        print(path)
        return 0
    _, config_dir = config.bridge_paths()
    problem = _workdir_problem(config, path, config_dir)
    if problem:
        print(f"[error] {problem}", file=sys.stderr)
        return 2
    try:
        result = Vault(path, config.language).init()
    except VaultError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    print(f"vault {path}")
    for name in result.created:
        print(f"created {clean_text(name, 80)}")
    for name in result.kept:
        print(f"kept {clean_text(name, 80)}")
    for name in result.skipped:
        print(f"skipped {clean_text(name, 80)} (symlink or not a regular file)")
    return 0


def _print_findings(report: object) -> None:
    from .plugins import shown

    for finding in report.findings:  # type: ignore[attr-defined]
        where = shown(finding.path) + (f":{finding.line}" if finding.line else "")
        print(f"{finding.severity:7}  {where}  {finding.rule}: {finding.message}")


def _print_components(report: object) -> None:
    for component in report.components:  # type: ignore[attr-defined]
        description = json.dumps(component.description, ensure_ascii=True)
        print(f"  {component.kind:7}  /{report.name}:{component.name}  {description}")  # type: ignore[attr-defined]


def _confirm(question: str) -> bool:
    try:
        if not sys.stdin.isatty():
            return False
        return input(question).strip().lower() in ("y", "yes", "j", "ja")
    except (EOFError, OSError, ValueError):
        return False


def _cmd_plugins(config: Config, args: argparse.Namespace) -> int:
    """List, check, enable or disable plugins (standard Claude Code plugin folders)."""
    from .plugins import PluginError, check_plugin, disable_plugin, enable_plugin, list_plugins, shown

    command = args.plugins_command
    if command == "list":
        plugins = list_plugins(config)
        if not plugins:
            print("no plugins (core plugin not found: set PA_CORE_PLUGIN_DIR)")
        for plugin in plugins:
            digest = (plugin.report.digest or "-")[:12]
            mark = " (shipped)" if plugin.shipped else ""
            print(f"{shown(plugin.name)}{mark}  {shown(plugin.version or '-')}  {plugin.status}  {digest}  "
                  f"{shown(plugin.path)}")
        return 0
    if command == "disable":
        try:
            disable_plugin(config, args.name)
        except PluginError as exc:
            print(f"[error] {exc}", file=sys.stderr)
            return 1
        print(f"disabled {shown(args.name)}; restart chat to unload it")
        return 0

    source = Path(args.dir).expanduser()
    if not os.path.lexists(source):
        print(f"[error] not found: {shown(source)}", file=sys.stderr)
        return 2
    report = check_plugin(source)
    if command == "check":
        _print_findings(report)
        errors = sum(f.severity == "error" for f in report.findings)
        warnings = len(report.findings) - errors
        name = shown(report.name or "?")
        verdict = "ok" if report.ok else "refused"
        print(f"{verdict}: {name} {shown(report.version or '-')}, {report.files} files, {report.total_bytes} bytes, "
              f"{len(report.components)} skills/commands/agents, {errors} errors, {warnings} warnings")
        if report.ok:
            _print_components(report)
        return 0 if report.ok else 1

    # enable
    if not report.ok:
        _print_findings(report)
        print("[error] plugin check failed: not enabled", file=sys.stderr)
        return 1
    _print_findings(report)  # warnings only
    print(f"plugin {shown(report.name)} {shown(report.version or '-')} adds:")
    _print_components(report)
    print("Skill descriptions are part of every request to the assistant.")
    if not args.yes and not _confirm("Enable this plugin? [y/N] "):
        print("[not enabled] (confirm in a terminal or pass --yes)", file=sys.stderr)
        return 1
    try:
        installed = enable_plugin(config, source, replace=args.replace, expected_digest=report.digest)
    except PluginError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    print(f"enabled {shown(installed.name)} {shown(installed.version or '-')}; restart chat to load it")
    return 0


def _cmd_brief(config: Config, args: argparse.Namespace) -> int:
    """Build today's brief without a model, write it to the vault, optionally polish and send it."""
    from .brief import run_brief

    return run_brief(config, send=args.send, polish=args.polish, by="manual",
                     out=None if args.quiet else print)


def _cmd_chat(config: Config, args: argparse.Namespace) -> int:
    """Talk to the assistant in the terminal. Writing asks for your approval first."""
    from .approval import PERMISSION_TOOL, ConsoleChannel
    from .approval_server import SERVER_NAME
    from .assistant import AssistantError, finish_assistant, prepare_assistant, reply_lines
    from .bridge import BridgeError
    from .console import ChatConsole
    from .remote import RemoteChannel, local_opener
    from .vault import HELPER_SYSTEM_PROMPT

    remote = bool(getattr(args, "remote_approvals", False))
    if remote and not config.api_token:
        print("[error] --remote-approvals needs PA_API_TOKEN (the same as for serve)", file=sys.stderr)
        return 2
    console_lock = threading.Lock()
    console = ChatConsole(console_lock)

    def make_channel(broker: object) -> object:
        def stop() -> bool:
            # Only the broker's end stops every prompt; each prompt's own scope decides the rest.
            return broker.closing.is_set()  # type: ignore[attr-defined]

        if remote:
            assert config.api_token
            try:
                with local_opener()(_local_url(config, "/healthz"), timeout=3) as response:
                    response.read(64)
            except (OSError, ValueError):
                print("[warning] `personal-agent serve` is not reachable: remote approvals will be denied")
            return RemoteChannel(_local_url(config, ""), config.api_token, console_lock, stop=stop,
                                 stop_for=broker.request_stopped)  # type: ignore[attr-defined]
        return ConsoleChannel(console_lock, stop=stop, stop_for=broker.request_stopped,  # type: ignore[attr-defined]
                              console=console, preempted=broker.request_preempted)  # type: ignore[attr-defined]

    try:
        parts = prepare_assistant(config, label="chat", channel=make_channel, note=print,
                                  warn=lambda line: print(line, file=sys.stderr))
    except AssistantError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return exc.code
    broker, workdir, config_dir = parts.broker, parts.workdir, parts.config_dir
    shared_stop = parts.latch.reason
    if shared_stop:
        # A safety stop of another session (e.g. serve's Telegram assistant) holds for this
        # chat too. Only the terminal lifts it, after an explicit confirmation.
        print(f"[safety] the assistant was stopped earlier ({shared_stop})")
        if _confirm("Clear this stop for every session and continue? [y/N] "):
            parts.latch.clear()
            print("[safety stop cleared]")
        else:
            print("[turns stay refused; `personal-agent safety clear` lifts the stop]")
    coordinator, notifier, service = _start_tasks(
        config, args, console, broker, parts.guard, parts.latch, parts.vault, workdir, config_dir,
        parts.mcp_timeout, HELPER_SYSTEM_PROMPT, PERMISSION_TOOL, SERVER_NAME, parts.domains,
    )
    # Delegation is offered (MCP flag, system prompt, init tripwire) only when the task
    # service really runs: otherwise the person would approve a call that can never run.
    delegation = service is not None and config.task_delegation

    def close_tasks() -> None:
        active = service.coordinator if service is not None else coordinator
        if active is not None:
            active.close()
        if notifier is not None:
            notifier.close()

    try:
        runtime = finish_assistant(
            parts, tasks=service, delegation=delegation, model=args.model or config.claude_model,
            # Only this typed tier-1 command may read `claude agents --json` (PA_SESSIONS_SOURCE).
            sessions=lambda: session_overview(config.status_dir, config.status_max_age_min,
                                              config.sessions_source, config.claude_bin),
        )
    except AssistantError as exc:
        close_tasks()
        broker.close()
        print(f"[error] {exc}", file=sys.stderr)
        return exc.code
    router = runtime.router
    say = console.say

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
    say(f"[vault {workdir} · writing asks first ({where}) · help for commands · /exit to quit]")
    try:
        while True:
            if service is not None and service.try_take_over():
                say("[tasks: this chat now runs the task board]")
            line = console.read_message("you> ")
            if line is None:
                break
            text = line.strip()
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
            for out_line in reply_lines(reply, config_dir):
                say(out_line)
    except KeyboardInterrupt:
        return 130
    finally:
        # No console lock here: a prompt thread may hold it; closing the broker ends that prompt.
        # The coordinator closes first, while the broker still runs, so its scopes close cleanly.
        active = service.coordinator if service is not None else coordinator
        if active is not None:
            active.close()
        router.close()
        broker.close()
        if notifier is not None:
            notifier.close()
        console.flush()
    return 0


def _start_tasks(config: Config, args: argparse.Namespace, console: object, broker: object, guard: object,
                 latch: object, vault: object, workdir: Path, config_dir: Path, mcp_timeout: dict[str, str],
                 helper_prompt_text: str, permission_tool: str, server_name: str,
                 domains: object = None) -> tuple:
    """The task board and, if this chat owns it, the coordinator. (coordinator, notifier, service)."""
    from .bridge import ClaudeSession
    from .coordinator import (
        Coordinator,
        TaskNotifier,
        TaskService,
        helper_bridge_config,
        helper_turn_timeout,
        notice_line,
    )
    from .notify import Notifier, NotifierPost, resolve_route
    from .tasks import TaskBoard, TaskError

    if not config.tasks_enabled:
        return None, None, None
    try:
        helper_prompt = broker.write_runtime_file("helper-system-prompt.md", helper_prompt_text)  # type: ignore[attr-defined]
    except OSError as exc:
        console.say(f"[warning] tasks are off: could not write the helper prompt ({type(exc).__name__})")  # type: ignore[attr-defined]
        return None, None, None
    board = TaskBoard(config.state_dir)
    notifier = None
    # Task notices outside the chat go through the notification layer (PA_NOTIFY_TASK_DONE;
    # by default Discord only with PA_TASK_DISCORD=counts). Sends are synchronous here, on
    # the TaskNotifier's own thread.
    messenger = Notifier.from_config(config, run_thread=False)
    # With a Telegram token the notifier is built even while the bot is unpaired: the channel
    # is resolved per notice, so a pairing made while this chat runs is picked up.
    if config.telegram_token or resolve_route(config, "task_done", messenger.telegram_ready()):
        notifier = TaskNotifier(NotifierPost(messenger, "task_done"), board, detail=config.notify_task_detail)
    elif config.task_discord == "counts" and not config.discord_webhook:
        console.say("[warning] PA_TASK_DISCORD=counts needs PA_DISCORD_WEBHOOK; task notices stay in the chat")  # type: ignore[attr-defined]

    def notify(event: object) -> None:
        console.notice(notice_line(event))  # type: ignore[arg-type, attr-defined]
        if notifier is not None:
            notifier.record(event)  # type: ignore[arg-type]

    model = config.task_model or args.model or config.claude_model

    def make_session(task: object, scope: object) -> ClaudeSession:
        cfg = helper_bridge_config(
            task, scope,  # type: ignore[arg-type]
            workdir=workdir, config_dir=config_dir, claude_bin=config.claude_bin, model=model,
            system_prompt_file=helper_prompt, permission_tool=permission_tool, server_name=server_name,
            oauth_token=config.claude_oauth_token,
            api_key=config.claude_api_key,
        )
        return ClaudeSession(cfg, extra_env=dict(mcp_timeout),
                             turn_timeout_s=helper_turn_timeout(config.approval_timeout_s))

    coordinator = Coordinator(
        board, broker, guard, latch, vault, make_session, notify,  # type: ignore[arg-type]
        max_parallel=config.task_parallel, task_timeout_s=config.task_timeout_s,
        task_quota_stop=config.task_quota_stop, max_age_s=config.task_max_age_h * 3600,
        domains=domains, domain_context_chars=config.domain_context_chars,  # type: ignore[arg-type]
    )
    try:
        aside = board.repair()
        if aside:
            console.say(f"[tasks: the task board was unreadable and was set aside as {aside}]")  # type: ignore[attr-defined]
        owner = coordinator.start()
    except TaskError as exc:
        console.say(f"[warning] tasks are off: {exc}")  # type: ignore[attr-defined]
        if notifier is not None:
            notifier.close()
        return None, None, None
    if owner:
        queued = sum(1 for t in board.load()[0] if t.status in ("queued", "waiting"))
        if queued:
            console.say(f"[tasks: {queued} queued]")  # type: ignore[attr-defined]
    else:
        console.say("[tasks: another chat owns the task board; tasks you add here run there]")  # type: ignore[attr-defined]
    # A chat that is not the owner keeps the coordinator on standby: when the owner chat
    # exits, this one takes over (TaskService.try_take_over) instead of leaving tasks queued.
    service = TaskService(board, coordinator if owner else None, latch, vault, domains,  # type: ignore[arg-type]
                          standby=None if owner else coordinator)
    return (coordinator if owner else None), notifier, service


HOOK_EVENTS = ("UserPromptSubmit", "PreToolUse", "Notification", "Stop", "StopFailure", "SessionEnd")


def _hook_default_status_dir() -> Path:
    """Where the status hook writes without PA_STATUS_DIR (same rule as in the hook)."""
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "personal-agent" / "sessions"


def hook_snippet(config: Config) -> dict:
    """The value of the "hooks" key of a Claude Code settings file for the status hook."""
    if not os.path.isabs(sys.executable or ""):
        raise ValueError("the Python interpreter path is not absolute")
    hook = Path(personal_agent.__file__).resolve().parent / "claude_status_hook.py"
    if not hook.is_file():
        raise ValueError("the packaged status hook is missing")
    command = shlex.join([sys.executable, "-I", "-S", str(hook)])
    status_dir = Path(os.path.abspath(config.status_dir.expanduser()))
    if status_dir != Path(os.path.abspath(_hook_default_status_dir().expanduser())):
        command = f"PA_STATUS_DIR={shlex.quote(str(status_dir))} {command}"
    return {event: [{"hooks": [{"type": "command", "command": command}]}] for event in HOOK_EVENTS}


def _cmd_hooks(config: Config, args: argparse.Namespace) -> int:
    """Print the settings snippet for the status hook. Never opens, reads or writes a settings file."""
    if args.hooks_command != "snippet":  # argparse allows only "snippet"
        return 2
    try:
        snippet = hook_snippet(config)
    except ValueError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2
    print(json.dumps(snippet, indent=2))
    print('Add this to the "hooks" section of your Claude Code settings (for example ~/.claude/settings.json).',
          file=sys.stderr)
    print("This command never edits any settings file.", file=sys.stderr)
    return 0


def _cmd_telegram(config: Config, args: argparse.Namespace) -> int:
    """Pair, inspect or unpair the Telegram bot. Never prints the bot token."""
    from .telegram import run_pairing

    command = args.telegram_command
    if not config.telegram_token:
        print("[error] PA_TELEGRAM_TOKEN is not set", file=sys.stderr)
        return 2
    if command == "pair":
        return run_pairing(config, force=args.force, yes=args.yes, print_code=args.print_code)
    if command == "unpair":
        return _telegram_unpair(config)
    return _telegram_status(config, args)


def _telegram_unpair(config: Config) -> int:
    from .telegram import OwnerStore, PairingStore, TelegramClient

    client = TelegramClient.from_config(config)
    removed = OwnerStore(config.state_dir, client.bot_id).remove()
    PairingStore(config.state_dir, client.bot_id).remove()
    print("unpaired; a running serve stops polling within 5 seconds" if removed else "not paired")
    return 0


def _telegram_status(config: Config, args: argparse.Namespace) -> int:
    from .notify import KINDS, resolve_route
    from .remote import PinLockout
    from .telegram import OwnerStore, PairingStore, PollLock, TelegramClient, TelegramError

    client = TelegramClient.from_config(config)
    lockout = PinLockout(config.state_dir)
    if args.clear_lock:
        lockout.clear()
        print("PIN lockouts cleared (a running serve picks this up at once)")
    print(f"token: set (bot id {client.bot_id})")
    if not args.offline:
        try:
            me = client.get_me()
            username = me.get("username") if isinstance(me.get("username"), str) else "?"
            print(f"bot: @{clean_text(username, 40)}")
            hook = client.get_webhook_info().get("url")
            print(f"webhook set: {'yes (another client may receive the updates; revoke the token)' if hook else 'no'}")
        except TelegramError as exc:
            print(f"bot: not reachable ({exc})")
    owner = OwnerStore(config.state_dir, client.bot_id).current()
    if owner is None:
        print("paired: no (personal-agent telegram pair)")
    else:
        paired_at = time.strftime("%Y-%m-%d %H:%M", time.localtime(owner.paired_at))
        print(f"paired: yes (chat {owner.masked_chat()}, since {paired_at})")
    pending = PairingStore(config.state_dir, client.bot_id).load()
    if pending is not None:
        print(f"pending pairing code: expires {time.strftime('%H:%M:%S', time.localtime(pending['expires_at']))}")
    print(f"poller running: {'yes' if PollLock(config.state_dir).held_elsewhere() else 'no'}")
    wrong, disabled = lockout.status()
    print(f"wrong approval PINs (24 h): {wrong}{' · level-3 remote approvals disabled' if disabled else ''}")
    ready = owner is not None
    for kind in KINDS:
        route = resolve_route(config, kind, ready)
        print(f"notices {kind}: {', '.join(route) if route else 'none'}")
    return 0


def _cmd_modules(config: Config, args: argparse.Namespace) -> int:
    """Access mode, usage guard and the modules this installation runs (starts no model)."""
    from .modules import describe
    from .spend import make_guard

    print(f"access mode: {config.access_mode}")
    print(f"usage: {make_guard(config).describe()}")
    for line in describe(config.modules):
        print(line)
    return 0


def _cmd_safety(config: Config, args: argparse.Namespace) -> int:
    """Show or clear the safety stop that every session of this agent shares."""
    from .router import clear_shared_stop, read_shared_stop

    if args.safety_command == "clear":
        print("safety stop cleared; restart serve and open chats (a running process keeps its stop)" if clear_shared_stop(config.state_dir)
              else "no safety stop is set")
        return 0
    reason = read_shared_stop(config.state_dir)
    print(f"safety stop: {reason}" if reason else "no safety stop is set")
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
        help="ask for approvals on your phone through `serve`: Telegram buttons when the bot is paired, "
             "else a Discord notice and a one-time link (PA_NOTIFY_APPROVAL)",
    )

    vault = sub.add_parser("vault", help="the Markdown vault the assistant works in")
    vault_sub = vault.add_subparsers(dest="vault_command", required=True)
    vault_sub.add_parser("init", help="create the vault skeleton (existing files are kept)")
    vault_sub.add_parser("path", help="print the vault path")

    plugins = sub.add_parser("plugins", help="checked Claude Code plugins (skills, commands, agents)")
    plugins_sub = plugins.add_subparsers(dest="plugins_command", required=True)
    plugins_sub.add_parser("list", help="list the core plugin and the enabled plugins")
    check = plugins_sub.add_parser("check", help="check a plugin folder without enabling it")
    check.add_argument("dir")
    enable = plugins_sub.add_parser("enable", help="check a plugin folder and copy it into the state directory")
    enable.add_argument("dir")
    enable.add_argument("--replace", action="store_true", help="replace an enabled plugin of the same name")
    enable.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    disable = plugins_sub.add_parser("disable", help="remove an enabled plugin")
    disable.add_argument("name")

    brief = sub.add_parser("brief", help="build today's brief (no model) and write it to the vault")
    brief.add_argument("--send", action="store_true",
                       help="send it to the brief channels (PA_NOTIFY_BRIEF; by default the paired Telegram bot, "
                            "else PA_DISCORD_WEBHOOK)")
    brief.add_argument("--polish", action="store_true",
                       help="let the small model rewrite the text once per day (respects the quota guard)")
    brief.add_argument("--quiet", action="store_true", help="do not print the brief")

    hooks = sub.add_parser("hooks", help="the status hook for your Claude Code sessions")
    hooks_sub = hooks.add_subparsers(dest="hooks_command", required=True)
    hooks_sub.add_parser("snippet", help="print the settings.json snippet (never edits any settings file)")

    telegram = sub.add_parser("telegram", help="the Telegram bot: pair, status, unpair")
    telegram_sub = telegram.add_subparsers(dest="telegram_command", required=True)
    pair = telegram_sub.add_parser("pair", help="print a one-time code and bind the chat that sends it")
    pair.add_argument("--force", action="store_true", help="replace an existing pairing")
    pair.add_argument("--yes", action="store_true", help="bind without asking (no terminal to confirm)")
    pair.add_argument("--print-code", action="store_true",
                      help="print the code even when stdout is not a terminal (it may end up in logs)")
    status = telegram_sub.add_parser("status", help="token, pairing, poller and notice routes (never the token)")
    status.add_argument("--offline", action="store_true", help="do not contact Telegram")
    status.add_argument("--clear-lock", action="store_true", help="reset the PIN lockout counter")
    telegram_sub.add_parser("unpair", help="forget the paired chat (a running serve stops polling)")

    sub.add_parser("modules", help="access mode, spend or quota, and the modules this installation runs")

    safety = sub.add_parser("safety", help="the safety stop shared by every session of this agent")
    safety_sub = safety.add_subparsers(dest="safety_command", required=True)
    safety_sub.add_parser("status", help="show the safety stop")
    safety_sub.add_parser("clear", help="clear the safety stop (terminal only)")
    return parser


COMMANDS = {
    "serve": _cmd_serve,
    "escalate": _cmd_escalate,
    "status": _cmd_status,
    "simulate": _cmd_simulate,
    "chat": _cmd_chat,
    "vault": _cmd_vault,
    "plugins": _cmd_plugins,
    "brief": _cmd_brief,
    "hooks": _cmd_hooks,
    "telegram": _cmd_telegram,
    "safety": _cmd_safety,
    "modules": _cmd_modules,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from .telegram import install_redaction

    install_redaction()  # bot tokens never reach a log line, tracebacks included
    try:
        config = Config.from_env()
        install_redaction(config)
        return COMMANDS[args.command](config, args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
