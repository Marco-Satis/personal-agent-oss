"""Shared construction of the assistant for the terminal chat and serve's Telegram assistant.

Both front ends get literally the same pieces: the workdir guard, the vault, the
checked plugins, an approval broker with its socket, the quota guard, a safety
latch shared with every other process of this agent, two locked-down Claude Code
sessions and the three-tier Router (tiers, slash refusal, tripwires, quota guard,
vault context). Only the approval channel, the trusted label of the main scope
("chat" or "telegram") and a few switches differ:

- the Telegram assistant never offers delegation and gets no agents listing;
- its Router lists no local sessions (`status` and `sessions` stay in the terminal);
- notes it captures carry a "[via telegram]" marker.

`prepare_assistant` builds everything up to the running broker; the caller then
starts its task service (the chat) or a board-only one (serve) and calls
`finish_assistant`, which writes the system prompt and creates the sessions and the
Router.
"""

from __future__ import annotations

import logging
import os
import shutil
import site
import sys
import sysconfig
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import personal_agent

from .config import Config
from .status import clean_text, read_statuses

if TYPE_CHECKING:
    from .approval import ApprovalBroker, ApprovalChannel
    from .bridge import ClaudeSession
    from .coordinator import TaskService
    from .domains import Domains
    from .quota import QuotaGuard
    from .router import Reply, Router, SafetyLatch
    from .vault import Vault

log = logging.getLogger(__name__)

# "Skill" lets the model use the skills of the checked plugins. The bridge routes every
# Skill call through the permission tool (HARDENED_SETTINGS), where the broker allows
# only the loaded plugins' skills; classify() would treat any other Skill route as level 3.
CHAT_TOOLS = ("Read", "Glob", "Grep", "Write", "Edit", "Skill")

STATE_ENTRIES = ("brief.json", "brief.lock", "brief-chunks.json", "brief-polished.json", "plugins.json", "quota.json",
                 "tasks.json", "tasks.lock", "tasks-owner.lock", "telegram-owner.json", "telegram-pairing.json",
                 "telegram-offset.json", "telegram.lock", "safety-stop.json", "approval-pinlock.json")
DELEGATION_PROMPT = """
You can queue background work with the tool mcp__pa__delegate_task (text up to 300 characters,
optional domain). Use it only when the person asks for background work. The person must approve
it. You never see the result: tell the person the task id; the result appears in
outputs/tasks/<id>.md and the person decides what to do with it.
"""
LOGIN_HINT_LOCAL = ("[the assistant uses its own Claude Code config in {config_dir}: "
                    "run `claude setup-token` once and set CLAUDE_CODE_OAUTH_TOKEN, "
                    "or point PA_CLAUDE_CONFIG_DIR at an existing login]")
LOGIN_HINT_REMOTE = ("[the assistant's Claude Code login is missing: in a terminal on the server, run "
                     "`claude setup-token` once and set CLAUDE_CODE_OAUTH_TOKEN]")


class AssistantError(Exception):
    """The assistant cannot start; code is the exit code the chat returns."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


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


def workdir_problem(config: Config, workdir: Path, config_dir: Path) -> str | None:
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


@dataclass
class AssistantParts:
    config: Config
    workdir: Path
    config_dir: Path
    vault: "Vault"
    plugins: tuple
    plugin_names: tuple[str, ...]
    allowed_skills: tuple[str, ...]
    broker: "ApprovalBroker"
    guard: "QuotaGuard"
    latch: "SafetyLatch"
    domains: "Domains"
    mcp_path: Path
    mcp_timeout: dict[str, str] = field(default_factory=dict)


def prepare_assistant(config: Config, *, label: str, channel: Callable[["ApprovalBroker"], "ApprovalChannel"],
                      note: Callable[[str], None], warn: Callable[[str], None] | None = None) -> AssistantParts:
    """The workdir check, the vault, the plugins and a running approval broker.

    label is the main scope's trusted label ("chat" or "telegram"); channel builds the
    approval channel for the broker. note gets the lines the chat prints ("[vault:
    created ...]", plugin warnings), warn the vault warning. Raises AssistantError.
    """
    from .approval import ApprovalBroker
    from .domains import Domains
    from .plugins import invocable, load_for_session
    from .spend import make_guard
    from .router import SafetyLatch
    from .vault import Vault, VaultError

    warn = warn or note
    workdir, config_dir = config.bridge_paths()
    problem = workdir_problem(config, workdir, config_dir)
    if problem:
        raise AssistantError(problem, 2)
    root = workdir.resolve()
    vault = Vault(workdir, config.language)
    try:
        created = vault.init().created
    except VaultError as exc:
        warn(f"[warning] {exc}")
        created = ()
    if created:
        note(f"[vault: created {', '.join(created)}]")
    plugins, warnings = load_for_session(config)  # checked on every start
    for warning in warnings:
        note(f"[{warning}]")
    plugin_names = tuple(p.name for p in plugins)
    allowed_skills = invocable(plugins)
    if plugins:
        note(f"[plugins: {', '.join(plugin_names)}]")

    broker = ApprovalBroker(workdir, config.approval_timeout_s, main_label=label)
    broker.allowed_skills = frozenset(allowed_skills)
    broker.channel = channel(broker)
    runtime_parent: Path | None = config.state_dir / "run"
    if _inside(runtime_parent.resolve(), root):
        runtime_parent = None
    try:
        mcp_path = broker.start(runtime_parent)
    except OSError as exc:
        raise AssistantError(f"could not start the approval service: {type(exc).__name__}", 1) from None
    if broker.runtime_dir is None or _inside(broker.runtime_dir.resolve(), root):
        broker.close()
        raise AssistantError("the approval runtime directory must not be inside the workdir", 2)
    return AssistantParts(
        config=config,
        workdir=workdir,
        config_dir=config_dir,
        vault=vault,
        plugins=tuple(plugins),
        plugin_names=plugin_names,
        allowed_skills=tuple(allowed_skills),
        broker=broker,
        guard=make_guard(config),
        latch=SafetyLatch(config.state_dir),  # one stop for this process and every other one
        domains=Domains(vault),
        mcp_path=mcp_path,
        mcp_timeout={"MCP_TOOL_TIMEOUT": str((config.approval_timeout_s + 30) * 1000)},
    )


@dataclass
class AssistantRuntime:
    """The running assistant: its sessions and its Router. One turn at a time."""

    parts: AssistantParts
    main_session: "ClaudeSession"
    small_session: "ClaudeSession | None"
    router: "Router"
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def turn(self, text: str, on_event: Callable[[dict], None] | None = None) -> "Reply":
        """One turn with the broker's turn bookkeeping. BridgeError propagates."""
        broker = self.parts.broker
        broker.begin_turn()
        try:
            return self.router.handle(text, on_event=on_event or broker.observe)
        finally:
            broker.end_turn()

    def reset_conversation(self) -> None:
        """A new conversation: the processes stop and the next turn starts fresh."""
        for session in (self.main_session, self.small_session):
            if session is not None:
                session.close(timeout_s=3)
                session.session_id = None

    def idle_close(self) -> None:
        """Stop the Claude Code processes but keep the conversation (the next turn resumes it)."""
        for session in (self.main_session, self.small_session):
            if session is not None and session.running:
                session.close(timeout_s=3)

    def close(self) -> None:
        """End a pending approval first (the broker), then stop the processes."""
        with self._lock:
            self.parts.broker.close()
            for session in (self.main_session, self.small_session):
                if session is not None:
                    session.close(timeout_s=3)


def finish_assistant(parts: AssistantParts, *, tasks: "TaskService | None", delegation: bool,
                     sessions: Callable[[], str] | None, model: str | None, local_listings: bool = True,
                     note_source: str | None = None) -> AssistantRuntime:
    """Write the system prompt and create the sessions and the Router. Raises AssistantError
    (the caller closes its task service and the broker)."""
    from .approval import PERMISSION_TOOL
    from .approval_server import SERVER_NAME
    from .bridge import DEFAULT_TURN_TIMEOUT_S, DELEGATE_TOOL, BridgeConfig, ClaudeSession
    from .router import Router, RouterConfig
    from .vault import VAULT_SYSTEM_PROMPT

    config, broker = parts.config, parts.broker
    delegation = bool(delegation and tasks is not None and broker.delegation_allowed)
    try:
        if delegation:
            broker.on_delegate = tasks.delegate  # type: ignore[union-attr]
            broker.enable_delegation()  # rewrites mcp.json before the main session starts
        # Outside the vault and 0600. Auto-loading of the vault's CLAUDE.md under
        # --restricted is not verified, so the layout rules travel this way.
        system_prompt = broker.write_runtime_file(
            "vault-system-prompt.md", VAULT_SYSTEM_PROMPT + (DELEGATION_PROMPT if delegation else ""))
    except OSError as exc:
        raise AssistantError(f"could not write the system prompt: {type(exc).__name__}", 1) from None

    main_session = ClaudeSession(
        BridgeConfig(
            workdir=parts.workdir,
            config_dir=parts.config_dir,
            claude_bin=config.claude_bin,
            model=model or config.claude_model,
            tools=CHAT_TOOLS,
            mcp_config=parts.mcp_path,
            permission_tool=PERMISSION_TOOL,
            system_prompt_file=system_prompt,
            plugin_dirs=tuple(p.path for p in parts.plugins),
            expected_plugins=parts.plugin_names,
            expected_mcp=(SERVER_NAME,),
            allowed_skills=parts.allowed_skills,
            oauth_token=config.claude_oauth_token,
            api_key=config.claude_api_key,
            extra_tools=(DELEGATE_TOOL,) if delegation else (),
        ),
        extra_env=dict(parts.mcp_timeout),
        turn_timeout_s=max(DEFAULT_TURN_TIMEOUT_S, config.approval_timeout_s + 120),
    )
    # Tier 2: no tools at all, no MCP servers, no permission tool, no plugins. Starts on first use.
    small_session = None
    if config.router_small_model:
        small_session = ClaudeSession(
            BridgeConfig(
                workdir=parts.workdir,
                config_dir=parts.config_dir,
                claude_bin=config.claude_bin,
                model=config.router_small_model,
                tools=(),
                oauth_token=config.claude_oauth_token,
                api_key=config.claude_api_key,
            )
        )
    router = Router(
        main_session,
        small_session,
        parts.guard,
        RouterConfig(config.router_small_model, config.router_small_max_chars),
        statuses=lambda: read_statuses(config.status_dir, config.status_max_age_min),
        vault=parts.vault,
        context_chars=config.vault_context_chars,
        slash_commands=frozenset("/" + name for name in parts.allowed_skills),
        on_context=broker.set_turn_context,
        latch=parts.latch,
        tasks=tasks,
        domains=parts.domains,
        sessions=sessions,
        local_listings=local_listings,
        note_source=note_source,
    )
    return AssistantRuntime(parts, main_session, small_session, router)


def _scrub_home(text: str) -> str:
    """Absolute paths under the home directory become "~/..." (no user name leaves the machine)."""
    home = str(Path.home())
    return text.replace(home, "~") if home not in ("", "/") else text


def reply_lines(reply: "Reply", config_dir: Path, *, prefix: str = "agent[{tier}]> ",
                local_paths: bool = True) -> list[str]:
    """The lines the chat prints for a reply. prefix "" for Telegram; local_paths=False keeps
    absolute local paths out of the lines (they would leave the machine)."""
    if reply.refused:
        lines = [f"[{'safety' if reply.safety else 'quota'}] {reply.text}"]
    else:
        lines = []
        if reply.context_count:
            lines.append(f"[context: {reply.context_count} notes]")
        lines.append(prefix.format(tier=reply.tier) + reply.text)
        result = reply.result
        if result is not None:
            for denial in result.denials:
                name = denial.get("tool_name") if isinstance(denial, dict) else None
                lines.append(f"[not approved: {clean_text(name or 'unknown tool', 80)}]")
            if result.is_error:
                lines.append(f"[turn ended with an error ({result.subtype})]")
                if "logged in" in result.text.lower():
                    lines.append(LOGIN_HINT_LOCAL.format(config_dir=config_dir) if local_paths
                                 else LOGIN_HINT_REMOTE)
    if not local_paths:
        lines = [_scrub_home(line) for line in lines]
    return lines


__all__ = [
    "CHAT_TOOLS", "DELEGATION_PROMPT", "STATE_ENTRIES", "AssistantError", "AssistantParts", "AssistantRuntime",
    "finish_assistant", "prepare_assistant", "reply_lines", "workdir_problem",
]
