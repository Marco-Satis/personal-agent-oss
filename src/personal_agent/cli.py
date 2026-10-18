"""Command line: serve, escalate, status, simulate, chat, vault, plugins, brief, hooks."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
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


# "Skill" lets the model use the skills of the checked plugins. The bridge routes every
# Skill call through the permission tool (HARDENED_SETTINGS), where the broker allows
# only the loaded plugins' skills; classify() would treat any other Skill route as level 3.
CHAT_TOOLS = ("Read", "Glob", "Grep", "Write", "Edit", "Skill")


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


STATE_ENTRIES = ("brief.json", "brief.lock", "brief-chunks.json", "brief-polished.json", "plugins.json", "quota.json",
                 "tasks.json", "tasks.lock", "tasks-owner.lock")
DELEGATION_PROMPT = """
You can queue background work with the tool mcp__pa__delegate_task (text up to 300 characters,
optional domain). Use it only when the person asks for background work. The person must approve
it. You never see the result: tell the person the task id; the result appears in
outputs/tasks/<id>.md and the person decides what to do with it.
"""


def _workdir_problem(config: Config, workdir: Path, config_dir: Path) -> str | None:
    """Reasons the assistant's working directory (the vault) is unsafe to use.

    The assistant may write inside the workdir (after approval). Nothing that runs
    code or that the agent trusts may live there: the package, the interpreter and
    its site-packages (.pth files run at startup), the Claude Code binary and
    config, the state, the session status files (they can trigger phone calls),
    the enabled plugins and the broker runtime directory (its MCP config holds the
    approval token). The vault may live inside the state directory (the default is
    state_dir/vault) as long as it contains no other state entry.
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
    state = config.state_dir.resolve()
    from .plugins import core_checkout_root, core_plugin_dir

    core = core_plugin_dir(config)
    checkout = core_checkout_root(config)
    inside_checks = [
        ("the broker runtime directory", state / "run"),
        ("the plugins directory", state / "plugins"),
        ("the Claude Code config directory", config_dir.resolve()),
    ]
    if core is not None:
        inside_checks.append(("the core plugin directory", core.resolve()))
    if checkout is not None:
        inside_checks.append(("the source checkout of the core plugin", checkout.resolve()))
    for name, path in inside_checks:
        if _inside(root, path):
            return f"vault must not be inside {name}"
    claude_bin = shutil.which(config.claude_bin)
    checks: list[tuple[str, Path]] = [
        ("the Claude Code config directory", config_dir),
        ("the state directory", config.state_dir),
        ("the session status directory", config.status_dir),
        ("the system temp directory", Path(tempfile.gettempdir())),
        ("the broker runtime directory", config.state_dir / "run"),
        ("the plugins directory", config.state_dir / "plugins"),
    ]
    checks += [(f"the state file {name}", config.state_dir / name) for name in STATE_ENTRIES]
    if core is not None:
        checks.append(("the core plugin directory", core))
    checks += [("the Python interpreter or its packages", p) for p in _python_paths()]
    if claude_bin:
        checks.append(("the Claude Code binary", Path(claude_bin)))
    for name, path in checks:
        for candidate in {path, path.resolve()}:  # a venv's python is often a symlink out
            if _inside(candidate.absolute(), root):
                return f"workdir must not contain {name}"
    return None


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
    from .approval import PERMISSION_TOOL, ApprovalBroker, ConsoleChannel
    from .approval_server import SERVER_NAME
    from .bridge import DEFAULT_TURN_TIMEOUT_S, DELEGATE_TOOL, BridgeConfig, BridgeError, ClaudeSession
    from .console import ChatConsole
    from .domains import Domains
    from .plugins import invocable, load_for_session
    from .quota import QuotaGuard
    from .remote import RemoteChannel, local_opener
    from .router import Router, RouterConfig, SafetyLatch
    from .vault import HELPER_SYSTEM_PROMPT, VAULT_SYSTEM_PROMPT, Vault, VaultError

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
    vault = Vault(workdir, config.language)
    try:
        created = vault.init().created
    except VaultError as exc:
        print(f"[warning] {exc}", file=sys.stderr)
        created = ()
    if created:
        print(f"[vault: created {', '.join(created)}]")
    plugins, warnings = load_for_session(config)  # checked on every start
    for warning in warnings:
        print(f"[{warning}]")
    plugin_names = tuple(p.name for p in plugins)
    allowed_skills = invocable(plugins)
    if plugins:
        print(f"[plugins: {', '.join(plugin_names)}]")

    console_lock = threading.Lock()
    console = ChatConsole(console_lock)
    broker = ApprovalBroker(workdir, config.approval_timeout_s)
    broker.allowed_skills = frozenset(allowed_skills)

    def stop() -> bool:
        # Only the broker's end stops every prompt; each prompt's own scope decides the rest.
        return broker.closing.is_set()

    if remote:
        assert config.api_token
        try:
            with local_opener()(_local_url(config, "/healthz"), timeout=3) as response:
                response.read(64)
        except (OSError, ValueError):
            print("[warning] `personal-agent serve` is not reachable: remote approvals will be denied")
        broker.channel = RemoteChannel(_local_url(config, ""), config.api_token, console_lock, stop=stop,
                                       stop_for=broker.request_stopped)
    else:
        broker.channel = ConsoleChannel(console_lock, stop=stop, stop_for=broker.request_stopped, console=console,
                                        preempted=broker.request_preempted)
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
    mcp_timeout = {"MCP_TOOL_TIMEOUT": str((config.approval_timeout_s + 30) * 1000)}
    latch = SafetyLatch()
    guard = QuotaGuard(config.quota_stop, config.state_dir / "quota.json")
    domains = Domains(vault)
    coordinator, notifier, service = _start_tasks(
        config, args, console, broker, guard, latch, vault, workdir, config_dir, mcp_timeout,
        HELPER_SYSTEM_PROMPT, PERMISSION_TOOL, SERVER_NAME, domains,
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
        if delegation:
            broker.on_delegate = service.delegate
            broker.enable_delegation()  # rewrites mcp.json before the main session starts
        # Outside the vault and 0600. Auto-loading of the vault's CLAUDE.md under
        # --restricted is not verified, so the layout rules travel this way.
        system_prompt = broker.write_runtime_file(
            "vault-system-prompt.md", VAULT_SYSTEM_PROMPT + (DELEGATION_PROMPT if delegation else ""))
    except OSError as exc:
        close_tasks()
        broker.close()
        print(f"[error] could not write the system prompt: {type(exc).__name__}", file=sys.stderr)
        return 1

    model = args.model or config.claude_model
    main_session = ClaudeSession(
        BridgeConfig(
            workdir=workdir,
            config_dir=config_dir,
            claude_bin=config.claude_bin,
            model=model,
            tools=CHAT_TOOLS,
            mcp_config=mcp_path,
            permission_tool=PERMISSION_TOOL,
            system_prompt_file=system_prompt,
            plugin_dirs=tuple(p.path for p in plugins),
            expected_plugins=plugin_names,
            expected_mcp=(SERVER_NAME,),
            allowed_skills=allowed_skills,
            oauth_token=config.claude_oauth_token,
            extra_tools=(DELEGATE_TOOL,) if delegation else (),
        ),
        extra_env=dict(mcp_timeout),
        turn_timeout_s=max(DEFAULT_TURN_TIMEOUT_S, config.approval_timeout_s + 120),
    )
    # Tier 2: no tools at all, no MCP servers, no permission tool, no plugins. Starts on first use.
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
        guard,
        RouterConfig(config.router_small_model, config.router_small_max_chars),
        statuses=lambda: read_statuses(config.status_dir, config.status_max_age_min),
        vault=vault,
        context_chars=config.vault_context_chars,
        slash_commands=frozenset("/" + name for name in allowed_skills),
        on_context=broker.set_turn_context,
        latch=latch,
        tasks=service,
        domains=domains,
        # Only this typed tier-1 command may read `claude agents --json` (PA_SESSIONS_SOURCE).
        sessions=lambda: session_overview(config.status_dir, config.status_max_age_min,
                                          config.sessions_source, config.claude_bin),
    )
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
            if reply.refused:
                say(f"[{'safety' if reply.safety else 'quota'}] {reply.text}")
                continue
            if reply.context_count:
                say(f"[context: {reply.context_count} notes]")
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
    from .remote import DiscordWebhook
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
    if config.task_discord == "counts":
        if config.discord_webhook:
            notifier = TaskNotifier(DiscordWebhook(config.discord_webhook), board)
        else:
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
    brief.add_argument("--send", action="store_true", help="post it to PA_DISCORD_WEBHOOK")
    brief.add_argument("--polish", action="store_true",
                       help="let the small model rewrite the text once per day (respects the quota guard)")
    brief.add_argument("--quiet", action="store_true", help="do not print the brief")

    hooks = sub.add_parser("hooks", help="the status hook for your Claude Code sessions")
    hooks_sub = hooks.add_subparsers(dest="hooks_command", required=True)
    hooks_sub.add_parser("snippet", help="print the settings.json snippet (never edits any settings file)")
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
