"""Three-tier router: answer as cheaply as possible, and never past the quota.

- Tier 1: fixed answers without any model (help, session status, quota).
- Tier 2: a small model in a separate session with no tools at all, only for
  short, self-contained questions.
- Tier 3: the main, locked-down session with file tools and approvals.

The heuristics are deliberately conservative: anything that mentions files,
code, notes or tasks, refers back to earlier turns, or is long goes to tier 3.
A leading "!" always forces tier 3. The quota guard runs before tiers 2 and 3,
so a refused turn never starts or reaches a model.

With a vault, "note <text>" is captured at tier 1 without any model, and tier 3
turns get the most relevant note paragraphs in front of the message, delimited
and escaped as reference data (see vault.render_context). Tier 2 never gets
notes. A message that starts with "/" would be expanded by Claude Code as a
slash command (built-in ones such as /config or /clear included), so the final
body of every turn may start with "/" only for an allowlisted plugin command.

"mail", "mail refresh" and "mail show <id>" are tier 1 too (mail.py): read-only, no
model, and on Telegram limited by PA_MAIL_TELEGRAM (the Router knows its channel).

Every turn goes through `guarded_send`, which watches the stream and stops the
turn on paid usage, on any hook event, on an init event that lists unexpected
tools, MCP servers or plugins, and on a Skill tool call outside the allowlist.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .bridge import MAX_PROMPT_CHARS, SKILL_TOOL, UnexpectedOutput
from .mail import NOT_CONFIGURED, MailCommand, parse_mail_command
from .quota import API_KEY_SOURCE, RESET_COMMAND, SUBSCRIPTION_KEY_SOURCE, QuotaGuard, is_overage
from .statefiles import read_private_json, remove_private, write_private_json
from .status import SessionStatus, clean_text, format_status_line, render_sessions, views_from_statuses
from .textsafe import is_unsafe
from .tasks import Command, parse_command
from .vault import Vault, VaultError, render_context

if TYPE_CHECKING:
    from .bridge import ClaudeSession, TurnResult
    from .coordinator import TaskService
    from .domains import Domains
    from .mail import MailService

log = logging.getLogger(__name__)

FORCE_PREFIX = "!"
LOCAL_ONLY = "Only in the terminal: session lists stay on this machine."
FIXED_COMMANDS = {
    "help": ("help", "/help", "?", "hilfe"),
    "status": ("status", "/status"),
    "quota": ("quota", "/quota", "usage", "kontingent"),
    "quota_reset": (RESET_COMMAND, "/" + RESET_COMMAND),
}
PREFIX_COMMANDS = {"note": ("note ", "notiz ", "/note ")}
NOTE_WORDS = ("note", "notiz", "/note")
CONTEXT_MARKER = "note context in this turn"
# Same text as approval.SKILL_CONTEXT: a "/<plugin>:<name>" message is expanded by the CLI
# itself, without a Skill tool call, so the broker would never see that a skill runs.
SKILL_MARKER = "plugin skill in this turn"
SIMPLE_STARTERS = (
    "what", "who", "when", "where", "which", "why", "how", "define", "translate", "explain",
    "convert", "calculate", "was", "wer", "wann", "wo", "welche", "welcher", "welches", "warum",
    "wieso", "wie", "übersetze", "erkläre", "definiere", "rechne",
)
TIER3_STEMS = (
    "file", "datei", "folder", "ordner", "director", "workspace", "writ", "schreib", "save",
    "speicher", "edit", "änder", "creat", "erstell", "delet", "lösch", "read", "lies", "öffn",
    "open", "search", "such", "find", "code", "script", "skript", "fix", "bug", "project",
    "projekt", "session", "note", "notiz", "remember", "merk", "todo", "task", "aufgabe",
)
REFERENCE_WORDS = frozenset((
    "it", "that", "this", "these", "those", "above", "previous", "earlier", "again", "continue",
    "dies", "diese", "dieses", "oben", "vorhin", "davon", "dazu", "damit", "darüber", "nochmal",
    "weiter",
))
FILE_LIKE = re.compile(r"[/\\`]|https?:|\b[\w-]+\.(md|txt|py|json|csv|ya?ml|toml|ini|log|html|js|ts)\b", re.I)
_WORDS = re.compile(r"[\wäöüß]+")
_TERMINATORS = re.compile(r"[.!?]")


@dataclass(frozen=True)
class RouterConfig:
    small_model: str | None = "haiku"  # None disables tier 2
    small_max_chars: int = 160


@dataclass(frozen=True)
class Reply:
    text: str
    tier: int
    is_error: bool = False
    refused: bool = False
    result: "TurnResult | None" = None
    context_count: int = 0
    safety: bool = False  # refused by a safety tripwire, not by the quota


class SafetyStop(Exception):
    """Raised from the event callback to abort a turn that is unsafe to continue."""


class PaidUsageStop(SafetyStop):
    """Raised from the event callback to abort a turn that would be billed."""


class UnsafeInput(SafetyStop):
    """Raised before anything is sent: the body must not reach Claude Code."""


SAFETY_FILE = "safety-stop.json"  # the cross-process stop in the state directory
SAFETY_FILE_VERSION = 1
SAFETY_FILE_CAP = 4096


def read_shared_stop(state_dir: Path) -> str | None:
    """The reason of the cross-process safety stop in state_dir, or None."""
    data = read_private_json(state_dir, SAFETY_FILE, SAFETY_FILE_CAP, SAFETY_FILE_VERSION)
    if data is None:
        return None
    reason = data.get("reason")
    return (clean_text(reason, 120) or "safety stop") if isinstance(reason, str) else "safety stop"


def clear_shared_stop(state_dir: Path) -> bool:
    """Remove the cross-process safety stop (terminal only). True if one was set."""
    present = os.path.lexists(Path(state_dir) / SAFETY_FILE)
    remove_private(Path(state_dir), SAFETY_FILE)
    return present


class SafetyLatch:
    """One safety stop for every session: the main session, all helpers and, with a state
    directory, every other process of this agent (the chat and serve's Telegram assistant).

    The sessions share the Claude Code config dir and login, so a hook or plugin
    surprise in one is a risk for all. The first reason wins; listeners (e.g. the
    task coordinator, which then stops its helpers) are told once. Thread-safe.

    With state_dir, trip() also writes state_dir/safety-stop.json, and `reason` adopts a
    stop that another process wrote (an mtime-cached read). Only the terminal clears that
    file (`personal-agent safety clear`, or a confirmed question at chat start).
    """

    def __init__(self, state_dir: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._reason: str | None = None
        self._listeners: list[Callable[[str], None]] = []
        self.state_dir = Path(state_dir) if state_dir is not None else None
        self._file_key: tuple[int, int, int] | None = None

    @property
    def shared(self) -> bool:
        return self.state_dir is not None

    def _set(self, reason: str, write: bool) -> None:
        with self._lock:
            if self._reason is not None:
                return
            self._reason = clean_text(reason, 120) or "safety stop"
            reason_text = self._reason
            listeners = list(self._listeners)
        log.warning("safety latch tripped: %s", reason_text)
        if write and self.state_dir is not None:
            write_private_json(self.state_dir, SAFETY_FILE,
                               {"version": SAFETY_FILE_VERSION, "reason": reason_text, "at": time.time()})
        for listener in listeners:
            try:
                listener(reason_text)
            except Exception as exc:  # noqa: BLE001 - a listener must never undo the stop
                log.warning("safety latch listener failed: %s", type(exc).__name__)

    def trip(self, reason: str) -> None:
        self._set(reason, write=True)

    def _check_shared(self) -> None:
        """Adopt a stop written by another process; the file is read only when it changed."""
        if self.state_dir is None:
            return
        try:
            info = os.stat(self.state_dir / SAFETY_FILE, follow_symlinks=False)
            key: tuple[int, int, int] | None = (info.st_mtime_ns, info.st_size, info.st_ino)
        except OSError:
            key = None
        with self._lock:
            if key == self._file_key or self._reason is not None:
                self._file_key = key
                return
            self._file_key = key
        if key is None:
            return
        shared = read_shared_stop(self.state_dir)
        if shared is not None:
            self._set(f"stopped in another session: {shared}", write=False)

    def clear(self) -> None:
        """Lift the stop in this process and remove the shared file (terminal confirmation only)."""
        if self.state_dir is not None:
            clear_shared_stop(self.state_dir)
        with self._lock:
            self._reason = None
            self._file_key = None

    def add_listener(self, listener: Callable[[str], None]) -> None:
        with self._lock:
            self._listeners.append(listener)
            reason = self._reason
        if reason is not None:
            listener(reason)

    @property
    def reason(self) -> str | None:
        self._check_shared()
        with self._lock:
            return self._reason

    def __bool__(self) -> bool:
        return self.reason is not None


def prefix_command(text: str) -> tuple[str, str] | None:
    """(command, argument) for a tier-1 prefix command such as "note <text>", else None.

    "note" alone gives an empty argument (usage). A leading "!" never matches.
    """
    s = text.strip()
    if s.startswith(FORCE_PREFIX):
        return None
    lowered = s.lower()
    for command, prefixes in PREFIX_COMMANDS.items():
        if lowered in NOTE_WORDS:
            return command, ""
        for prefix in prefixes:
            if lowered.startswith(prefix):
                return command, s[len(prefix):].strip()
    return None


def strip_leading(body: str) -> str:
    """body without leading whitespace, control and invisible characters and "!" marks.

    "!" is skipped too, so a doubled force prefix ("!!/config") is still seen as a
    slash command.
    """
    index = 0
    while index < len(body) and (body[index].isspace() or body[index] == FORCE_PREFIX or is_unsafe(body[index])):
        index += 1
    return body[index:]


def slash_problem(body: str, allowed: frozenset[str]) -> str | None:
    """Why body must not reach Claude Code as a slash command, or None.

    Leading whitespace and invisible characters are ignored, so "! /clear" or a
    zero-width space before "/config" are caught too. A body starting with "/" is
    allowed only when its first word exactly matches an allowlisted "/<plugin>:<name>";
    a bare "/name" is refused even if the CLI could resolve it.
    """
    probe = strip_leading(body)
    if not probe.startswith("/"):
        return None
    if probe.split(None, 1)[0] in allowed:
        return None
    return "unknown command; plugin skills are /<plugin>:<skill>"


def choose_tier(text: str, config: RouterConfig) -> tuple[int, str]:
    """Pick the tier for one message and return it with the text to send. Doubt means 3."""
    s = text.strip()
    if s.startswith(FORCE_PREFIX):
        return 3, s[len(FORCE_PREFIX):].strip()
    lowered = s.lower()
    for key, words in FIXED_COMMANDS.items():
        if lowered in words:
            return 1, key
    if not config.small_model or not s or len(s) > config.small_max_chars or "\n" in s:
        return 3, s
    if len(_TERMINATORS.findall(s)) > 1 or FILE_LIKE.search(s):
        return 3, s
    tokens = _WORDS.findall(lowered)
    if not tokens:
        return 3, s
    if any(token.startswith(TIER3_STEMS) for token in tokens):
        return 3, s
    if any(token in REFERENCE_WORDS for token in tokens):
        return 3, s
    if s.endswith("?") or tokens[0] in SIMPLE_STARTERS:
        return 2, s
    return 3, s


# --- safety tripwires -------------------------------------------------------------


def _resolved(path: object) -> str | None:
    try:
        return os.path.realpath(os.fspath(path))  # type: ignore[arg-type]
    except (TypeError, ValueError, OSError):
        return None


def _named_list(event: dict, key: str, strict: bool, what: str) -> tuple[list, str | None]:
    """The init list under key; a missing list is a problem only for strict sessions."""
    value = event.get(key)
    if isinstance(value, list):
        return value, None
    if value is None and not strict:
        return [], None
    return [], f"the init event does not list the {what}"


def init_problem(event: dict, config: object) -> str | None:
    """Why an init event shows a session we did not configure, or None.

    Allowed: the tools of config.tools plus the permission tool, the MCP servers in
    config.expected_mcp, built-in plugins and the plugins in config.expected_plugins
    loaded from one of config.plugin_dirs. A session with plugins or MCP servers fails
    closed when a list is missing or malformed.
    """
    expected_plugins = frozenset(getattr(config, "expected_plugins", ()) or ())
    expected_mcp = frozenset(getattr(config, "expected_mcp", ()) or ())
    plugin_dirs = tuple(getattr(config, "plugin_dirs", ()) or ())
    tools = set(getattr(config, "tools", ()) or ()) | set(getattr(config, "extra_tools", ()) or ())
    permission_tool = getattr(config, "permission_tool", None)
    if permission_tool:
        tools.add(permission_tool)
    strict = bool(plugin_dirs or expected_mcp)

    listed, problem = _named_list(event, "tools", strict, "tools")
    if problem:
        return problem
    for name in listed:
        if not isinstance(name, str):
            return "the init event lists a tool without a name"
        if name not in tools:
            return f"an unexpected tool was loaded ({clean_text(name, 60)})"

    listed, problem = _named_list(event, "mcp_servers", strict, "MCP servers")
    if problem:
        return problem
    for server in listed:
        name = server.get("name") if isinstance(server, dict) else None
        if not isinstance(name, str):
            return "the init event lists an MCP server without a name"
        if name not in expected_mcp:
            return f"an unexpected MCP server was loaded ({clean_text(name, 60)})"

    listed, problem = _named_list(event, "plugins", strict, "plugins")
    if problem:
        return problem
    dirs = {_resolved(path) for path in plugin_dirs} - {None}
    builtin: set[str] = set()
    for plugin in listed:
        if not isinstance(plugin, dict):
            return "the init event lists a plugin without a name"
        name, source = plugin.get("name"), plugin.get("source")
        if not isinstance(name, str) or not isinstance(source, str):
            return "the init event lists a plugin without a name or source"
        if source.endswith("@builtin"):
            builtin.add(name)
            continue
        if name not in expected_plugins:
            return f"an unexpected plugin was loaded ({clean_text(name, 60)})"
        if _resolved(plugin.get("path")) not in dirs:
            return f"plugin {clean_text(name, 60)} was loaded from an unexpected path"

    namespaces = expected_plugins | builtin
    for key in ("skills", "slash_commands", "agents"):
        entries = event.get(key)
        for entry in entries if isinstance(entries, list) else ():
            if isinstance(entry, str) and ":" in entry and entry.split(":", 1)[0].lstrip("/") not in namespaces:
                return f"the init event lists {key} of an unexpected plugin ({clean_text(entry, 60)})"
    return None


def skill_problem(event: dict, allowed: frozenset[str]) -> str | None:
    """Why an assistant event must not continue: a Skill call outside the allowlist."""
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    for block in content if isinstance(content, list) else ():
        if not isinstance(block, dict) or block.get("type") != "tool_use" or block.get("name") != SKILL_TOOL:
            continue
        tool_input = block.get("input")
        skill = tool_input.get("skill") if isinstance(tool_input, dict) else None
        if not isinstance(skill, str) or skill.lstrip("/") not in allowed:
            return f"the assistant tried to load a skill that is not allowed ({clean_text(skill, 60)})"
    return None


def guarded_send(
    session: "ClaudeSession",
    body: str,
    guard: QuotaGuard,
    on_event: Callable[[dict], None] | None = None,
    slash_commands: frozenset[str] | None = None,
) -> "TurnResult":
    """Send one turn and stop it on anything that is paid or unexpected.

    Raises UnsafeInput (nothing sent) when the body would run a slash command that is
    not allowlisted; the default allowlist is "/" plus session.config.allowed_skills.
    Raises PaidUsageStop on an API key source or paid overage (access mode
    "subscription"), SafetyStop when an "api" session reports any other access path,
    and SafetyStop on any
    hook event, on an init event with unexpected tools, MCP servers, plugins or plugin
    skills, and on a Skill tool call outside session.config.allowed_skills. The caller
    then closes the session right away.

    This is a tripwire, not a boundary: UserPromptSubmit hooks emit no stream event,
    and a SessionStart hook has already run when its event arrives. The boundary is
    the plugin checker plus disableAllHooks, the Skill permission rule and
    --strict-mcp-config (bridge.HARDENED_SETTINGS).
    """
    config = getattr(session, "config", None)
    allowed = frozenset(getattr(config, "allowed_skills", ()) or ())
    api_mode = bool(getattr(config, "api_key", None))
    if slash_commands is None:
        slash_commands = frozenset("/" + name for name in allowed)
    problem = slash_problem(body, slash_commands)
    if problem:
        raise UnsafeInput(problem)

    def wrapped(event: dict) -> None:
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "system":
            subtype = event.get("subtype")
            if isinstance(subtype, str) and subtype.startswith("hook"):
                raise SafetyStop("a hook ran inside the assistant session")
            if subtype == "init":
                source = event.get("apiKeySource")
                if api_mode:
                    # The installation pays per token on purpose; anything else means a
                    # second access path (a login) slipped in.
                    if source != API_KEY_SOURCE:
                        raise SafetyStop(
                            f"Claude Code reports access '{clean_text(str(source), 40)}' "
                            "instead of the configured API key"
                        )
                elif source is not None and source != SUBSCRIPTION_KEY_SOURCE:
                    reason = (
                        f"Claude Code reports API key source '{clean_text(source, 40)}', "
                        "which is billed per token instead of the subscription"
                    )
                    guard.block_paid(f"{reason}; fix the login and restart")
                    raise PaidUsageStop(reason)
                problem = init_problem(event, config)
                if problem:
                    raise SafetyStop(problem)
        elif kind == "assistant":
            problem = skill_problem(event, allowed)
            if problem:
                raise SafetyStop(problem)
        elif kind == "rate_limit_event":
            info = event.get("rate_limit_info")
            guard.observe(info)
            if not api_mode and is_overage(info):
                raise PaidUsageStop("paid overage is in use")
        if on_event is not None:
            on_event(event)

    try:
        result = session.send(body, on_event=wrapped)
    except UnexpectedOutput as exc:
        raise SafetyStop(str(exc)) from exc
    record = getattr(guard, "record_cost", None)  # spend.SpendGuard (access mode "api")
    if record is not None:
        record(session, getattr(result, "cost_usd", None))
    return result


class Router:
    """Routes chat messages to fixed answers, the small session or the main session."""

    def __init__(
        self,
        main: "ClaudeSession",
        small: "ClaudeSession | None",
        guard: QuotaGuard,
        config: RouterConfig,
        statuses: Callable[[], list[SessionStatus]],
        vault: Vault | None = None,
        context_chars: int = 0,
        slash_commands: frozenset[str] = frozenset(),
        on_context: Callable[[str], None] | None = None,
        latch: SafetyLatch | None = None,
        tasks: "TaskService | None" = None,
        domains: "Domains | None" = None,
        sessions: Callable[[], str] | None = None,
        local_listings: bool = True,
        note_source: str | None = None,
        channel: str = "console",
        mail: "MailService | None" = None,
    ) -> None:
        self.main = main
        # Trusted: "telegram" for serve's Telegram assistant. Mail output follows PA_MAIL_TELEGRAM there.
        self.channel = "telegram" if channel == "telegram" else "console"
        self.mail = mail  # None: PA_MAIL_CONFIG is not set
        self.note_source = note_source  # trusted: "telegram" marks notes captured over Telegram
        self.small = small if config.small_model else None
        self.guard = guard
        self.config = config
        self.statuses = statuses
        self.vault = vault
        self.context_chars = max(0, context_chars)
        self.slash_commands = frozenset(slash_commands)  # exact "/<plugin>:<name>" words
        self.on_context = on_context  # told before a turn with notes or a plugin command is sent
        # Shared with the task coordinator: a safety stop in a helper refuses further
        # turns here, and a stop here pauses and stops the helpers.
        self.latch = latch if latch is not None else SafetyLatch()
        self.tasks = tasks  # None: PA_TASKS=off
        self.domains = domains  # None: no vault
        # The read-only `sessions` view; None: built from the hook status files only.
        self.sessions = sessions
        # False (serve's Telegram assistant): `status` and `sessions` list nothing, because
        # project names and waiting texts of every Claude Code session would leave the machine.
        self.local_listings = local_listings

    @property
    def halted(self) -> str | None:
        """The safety-stop reason; later turns are refused."""
        return self.latch.reason

    @halted.setter
    def halted(self, reason: str | None) -> None:
        if reason:
            self.latch.trip(reason)

    # --- tier 1 ---------------------------------------------------------------

    def help_text(self) -> str:
        lines = [
            "Commands answered without a model: help, status (your Claude Code sessions), "
            "sessions (the same, waiting ones first; read-only), "
            f"quota (subscription usage), {RESET_COMMAND} (forget the saved usage data), /exit.",
        ]
        if self.small is not None:
            lines.append(
                "Short, simple questions go to a small model without tools (agent[2]); "
                "everything else goes to the main assistant (agent[3])."
            )
        else:
            lines.append("Messages go to the main assistant (agent[3]).")
        lines.append(f'Start a message with "{FORCE_PREFIX}" to send it to the main assistant directly.')
        if self.vault is not None:
            lines.append(
                '"note <text>" saves a note to the vault inbox without a model.'
                + (" Relevant notes are attached to messages for the main assistant." if self.context_chars else "")
            )
        if self.slash_commands:
            lines.append("Plugin skills: " + ", ".join(sorted(self.slash_commands)) + " (e.g. /core:<skill>).")
        if self.tasks is not None:
            lines.append(
                "Background tasks: task [@domain] <text> hands work to a helper; tasks lists them; "
                "task show <id>, task cancel <id>. Results go to outputs/tasks/<id>.md."
            )
        if self.domains is not None:
            lines.append(
                "Domains (memory per topic for tasks, wiki/domains/<name>.md): domains lists them; "
                "domain <name> shows one."
            )
        if self.mail is not None:
            line = self.mail.help_line(self.channel)
            if line:
                lines.append(line)
        return "\n".join(lines)

    def _mail_command(self, command: MailCommand) -> Reply:
        """Tier 1: no model, no quota, works while halted. Never sends anything."""
        if self.mail is None:
            return Reply(NOT_CONFIGURED, 1, is_error=True)
        try:
            text, is_error = self.mail.command(command, self.channel)
        except Exception as exc:  # noqa: BLE001 - a broken mail setup must not end the chat
            log.warning("mail command failed: %s", type(exc).__name__)
            return Reply(f"mail unavailable: {type(exc).__name__}", 1, is_error=True)
        return Reply(text, 1, is_error=is_error)

    def _domain_command(self, command: "Command") -> Reply:
        if command.kind == "domain_usage":
            return Reply("usage: domains · domain <name> (lowercase letters, digits and dashes)", 1)
        if self.domains is None:
            return Reply("domains need a vault", 1, is_error=True)
        try:
            if command.kind == "domains":
                return Reply(self.domains.list_text(), 1)
            return Reply(self.domains.show(command.arg), 1)
        except (OSError, VaultError) as exc:
            return Reply(f"domains unavailable: {type(exc).__name__}", 1, is_error=True)

    def _sessions(self) -> Reply:
        """The person's Claude Code sessions, waiting first. Read-only: nothing is sent anywhere."""
        if not self.local_listings:
            return Reply(LOCAL_ONLY, 1)
        try:
            if self.sessions is not None:
                return Reply(self.sessions(), 1)
            return Reply(render_sessions(views_from_statuses(self.statuses())), 1)
        except Exception as exc:  # noqa: BLE001 - a broken source must not end the chat
            return Reply(f"sessions unavailable: {type(exc).__name__}", 1, is_error=True)

    def _task_command(self, command: "Command") -> Reply:
        if command.kind == "sessions":
            return self._sessions()
        if command.kind.startswith("domain"):
            return self._domain_command(command)
        if command.kind == "task_usage":
            return Reply("usage: task [@domain] <text> · tasks · task show <id> · task cancel <id>", 1)
        if self.tasks is None:
            return Reply("tasks are off (PA_TASKS=off)", 1, is_error=True)
        text, is_error = self.tasks.command(command)
        return Reply(text, 1, is_error=is_error)

    def _note(self, argument: str) -> Reply:
        if not argument:
            return Reply("usage: note <text>", 1)
        if self.vault is None:
            return Reply("no vault configured", 1, is_error=True)
        try:
            path = self.vault.capture(argument, source=self.note_source) if self.note_source else \
                self.vault.capture(argument)
        except VaultError as exc:
            return Reply(str(exc), 1, is_error=True)
        return Reply(f"noted in {path}", 1)

    def _with_context(self, body: str) -> tuple[str, int]:
        """The body with relevant vault notes in front, or the plain body."""
        if self.vault is None or self.context_chars <= 0:
            return body, 0
        try:
            excerpts = self.vault.select_context(body, self.context_chars)
        except Exception as exc:  # noqa: BLE001 - notes are optional, the turn must still work
            log.warning("vault notes skipped: %s", type(exc).__name__)
            return body, 0
        if not excerpts:
            return body, 0
        text = render_context(body, excerpts)
        if len(text.strip()) > MAX_PROMPT_CHARS:
            return body, 0
        return text, len(excerpts)

    def _fixed(self, command: str) -> str:
        if command == "help":
            return self.help_text()
        if command == "status":
            if not self.local_listings:
                return LOCAL_ONLY
            statuses = self.statuses()
            if not statuses:
                return "no session status"
            return "\n".join(format_status_line(s) for s in statuses)
        if command == "quota_reset":
            self.guard.reset()
            return "quota data cleared; the next turn refreshes it. " + self.guard.describe()
        return self.guard.describe()

    # --- tiers 2 and 3 ----------------------------------------------------------

    def is_tier1(self, text: str) -> bool:
        """Whether handle(text) answers without any model (notes, task commands, fixed answers)."""
        if prefix_command(text) is not None or parse_command(text) is not None \
                or parse_mail_command(text) is not None:
            return True
        tier, body = choose_tier(text, self.config)
        return tier == 1 or not body or slash_problem(body, self.slash_commands) is not None

    def handle(self, text: str, on_event: Callable[[dict], None] | None = None) -> Reply:
        prefixed = prefix_command(text)
        if prefixed is not None:
            return self._note(prefixed[1])
        command = parse_command(text)
        if command is not None:
            return self._task_command(command)  # tier 1: no quota, works while halted
        mail_command = parse_mail_command(text)
        if mail_command is not None:
            return self._mail_command(mail_command)
        tier, body = choose_tier(text, self.config)
        if tier == 1:
            return Reply(self._fixed(body), 1)
        if not body:
            return Reply(f'nothing to send after "{FORCE_PREFIX}"', 1, is_error=True)
        problem = slash_problem(body, self.slash_commands)
        if problem:
            return Reply(problem, 1, is_error=True)
        slash = strip_leading(body).startswith("/")
        if slash:
            body = strip_leading(body)  # verbatim, so the CLI sees the leading "/"
        halted = self.halted
        if halted:
            hint = " A stop shared by all sessions is cleared in a terminal: personal-agent safety clear." \
                if self.latch.shared else ""
            return Reply(
                f"Not sent: the assistant was stopped earlier ({halted}). Restart the chat.{hint}",
                1, is_error=True, refused=True, safety=True,
            )
        check = self.guard.check()
        if not check.ok:
            return Reply(f"Not sent: {check.reason}. Commands help/status/quota still work.", 1, refused=True)
        if slash:
            tier = 3  # plugin commands need the main session and are sent verbatim
        session = self.small if tier == 2 else self.main
        if session is None:
            tier, session = 3, self.main
        count = 0
        if slash and self.on_context is not None:
            self.on_context(SKILL_MARKER)
        if tier == 3 and not slash:
            body, count = self._with_context(body)
            if count and self.on_context is not None:
                self.on_context(CONTEXT_MARKER)
        if slash:
            # Skills otherwise date notes "00:00": the model has no clock of its own. The time
            # goes after the command, so the CLI still sees the leading "/" and the skill gets
            # it as part of its arguments.
            body = f"{body} (local time: {time.strftime('%Y-%m-%d %H:%M %A')})"
        slash_commands = self.slash_commands if session is self.main else frozenset()
        try:
            result = guarded_send(session, body, self.guard, on_event, slash_commands)
        except SafetyStop as exc:
            log.warning("turn aborted: %s", exc)
            session.close(timeout_s=0)  # stop the process right away, mid-turn
            paid = isinstance(exc, PaidUsageStop)
            if not paid and not isinstance(exc, UnsafeInput):
                # A hook or an unexpected plugin may run again on the next start: stay stopped.
                self.halted = clean_text(str(exc), 120)
            return Reply(f"Stopped: {exc}. The turn was aborted.", tier, is_error=True, refused=True, safety=not paid)
        return Reply(result.text, tier, result.is_error, result=result, context_count=count)

    def close(self) -> None:
        for session in (self.main, self.small):
            if session is not None:
                session.close()
