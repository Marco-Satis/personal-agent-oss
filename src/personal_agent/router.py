"""Three-tier router: answer as cheaply as possible, and never past the quota.

- Tier 1: fixed answers without any model (help, session status, quota).
- Tier 2: a small model in a separate session with no tools at all, only for
  short, self-contained questions.
- Tier 3: the main, locked-down session with file tools and approvals.

The heuristics are deliberately conservative: anything that mentions files,
code, notes or tasks, refers back to earlier turns, or is long goes to tier 3.
A leading "!" always forces tier 3. The quota guard runs before tiers 2 and 3,
so a refused turn never starts or reaches a model.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from .quota import RESET_COMMAND, SUBSCRIPTION_KEY_SOURCE, QuotaGuard, is_overage
from .status import SessionStatus, clean_text, format_status_line

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


class PaidUsageStop(Exception):
    """Raised from the event callback to abort a turn that would be billed."""


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


class Router:
    """Routes chat messages to fixed answers, the small session or the main session."""

    def __init__(
        self,
        main: "ClaudeSession",
        small: "ClaudeSession | None",
        guard: QuotaGuard,
        config: RouterConfig,
        statuses: Callable[[], list[SessionStatus]],
    ) -> None:
        self.main = main
        self.small = small if config.small_model else None
        self.guard = guard
        self.config = config
        self.statuses = statuses

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
        return "\n".join(lines)

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

    def _watch(self, on_event: Callable[[dict], None] | None) -> Callable[[dict], None]:
        def wrapped(event: dict) -> None:
            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "init":
                source = event.get("apiKeySource")
                if source is not None and source != SUBSCRIPTION_KEY_SOURCE:
                    reason = (
                        f"Claude Code reports API key source '{clean_text(source, 40)}', "
                        "which is billed per token instead of the subscription"
                    )
                    self.guard.block_paid(f"{reason}; fix the login and restart")
                    raise PaidUsageStop(reason)
            elif kind == "rate_limit_event":
                info = event.get("rate_limit_info")
                self.guard.observe(info)
                if is_overage(info):
                    raise PaidUsageStop("paid overage is in use")
            if on_event is not None:
                on_event(event)

        return wrapped

    def handle(self, text: str, on_event: Callable[[dict], None] | None = None) -> Reply:
        tier, body = choose_tier(text, self.config)
        if tier == 1:
            return Reply(self._fixed(body), 1)
        if not body:
            return Reply(f'nothing to send after "{FORCE_PREFIX}"', 1, is_error=True)
        check = self.guard.check()
        if not check.ok:
            return Reply(f"Not sent: {check.reason}. Commands help/status/quota still work.", 1, refused=True)
        session = self.small if tier == 2 else self.main
        if session is None:
            tier, session = 3, self.main
        try:
            result = session.send(body, on_event=self._watch(on_event))
        except PaidUsageStop as exc:
            log.warning("turn aborted: %s", exc)
            session.close(timeout_s=0)  # stop the process right away, mid-turn
            return Reply(f"Stopped: {exc}. The turn was aborted.", tier, is_error=True, refused=True)
        return Reply(result.text, tier, result.is_error, result=result)

    def close(self) -> None:
        for session in (self.main, self.small):
            if session is not None:
                session.close()
