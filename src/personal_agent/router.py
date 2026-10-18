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

Every turn goes through `guarded_send`, which watches the stream and stops the
turn on paid usage, on any hook event, on an init event that lists unexpected
tools, MCP servers or plugins, and on a Skill tool call outside the allowlist.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from .bridge import MAX_PROMPT_CHARS, SKILL_TOOL
from .quota import RESET_COMMAND, SUBSCRIPTION_KEY_SOURCE, QuotaGuard, is_overage
from .status import SessionStatus, clean_text, format_status_line
from .textsafe import is_unsafe
from .vault import Vault, VaultError, render_context

if TYPE_CHECKING:
    from .bridge import ClaudeSession, TurnResult

log = logging.getLogger(__name__)

FORCE_PREFIX = "!"
FIXED_COMMANDS = {
    "help": ("help", "/help", "?", "hilfe"),
    "status": ("status", "/status", "sessions"),
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
    tools = set(getattr(config, "tools", ()) or ())
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
    Raises PaidUsageStop on an API key source or paid overage, and SafetyStop on any
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
                if source is not None and source != SUBSCRIPTION_KEY_SOURCE:
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
            if is_overage(info):
                raise PaidUsageStop("paid overage is in use")
        if on_event is not None:
            on_event(event)

    return session.send(body, on_event=wrapped)


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
    ) -> None:
        self.main = main
        self.small = small if config.small_model else None
        self.guard = guard
        self.config = config
        self.statuses = statuses
        self.vault = vault
        self.context_chars = max(0, context_chars)
        self.slash_commands = frozenset(slash_commands)  # exact "/<plugin>:<name>" words
        self.on_context = on_context  # told before a turn with notes or a plugin command is sent
        self.halted: str | None = None  # set by a safety stop; later turns are refused

    # --- tier 1 ---------------------------------------------------------------

    def help_text(self) -> str:
        lines = [
            "Commands answered without a model: help, status (your Claude Code sessions), "
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
        return "\n".join(lines)

    def _note(self, argument: str) -> Reply:
        if not argument:
            return Reply("usage: note <text>", 1)
        if self.vault is None:
            return Reply("no vault configured", 1, is_error=True)
        try:
            path = self.vault.capture(argument)
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
            statuses = self.statuses()
            if not statuses:
                return "no session status"
            return "\n".join(format_status_line(s) for s in statuses)
        if command == "quota_reset":
            self.guard.reset()
            return "quota data cleared; the next turn refreshes it. " + self.guard.describe()
        return self.guard.describe()

    # --- tiers 2 and 3 ----------------------------------------------------------

    def handle(self, text: str, on_event: Callable[[dict], None] | None = None) -> Reply:
        prefixed = prefix_command(text)
        if prefixed is not None:
            return self._note(prefixed[1])
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
        if self.halted:
            return Reply(
                f"Not sent: the assistant was stopped earlier ({self.halted}). Restart the chat.",
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
