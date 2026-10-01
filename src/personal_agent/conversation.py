"""The phone conversation as a transport-independent state machine.

The same `Call` object is driven by the Twilio webhooks and by the local
simulator. It is deliberately deterministic: no language model decides what
happens on the phone in v1, so nothing a session or an e-mail writes can turn
into an action.

Flow: PIN check -> alert -> menu (status / repeat / acknowledge / goodbye).
Nothing about the alert is said before the PIN is correct.
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .status import SessionStatus

MAX_PIN_ATTEMPTS = 3
MAX_SILENT_TURNS = 2
MAX_SESSIONS_READ = 5

TEXT = {
    "en": {
        "greeting": "Hello, this is your agent. Please enter your PIN, followed by the hash key.",
        "pin_wrong": "That PIN is not correct. Please try again.",
        "pin_locked": "Too many wrong attempts. Goodbye.",
        "alert": "Alert from {source}: {summary}",
        "menu": "Press 1 or say status for your sessions. Press 2 to repeat the alert. "
        "Press 3 to acknowledge it. Press 9 or say goodbye to hang up.",
        "no_sessions": "No session has reported a status recently.",
        "sessions_head": "{count} sessions reported.",
        "session_line": "{project} is {state} since {age} minutes. {summary}",
        "more_sessions": "And {count} more.",
        "acknowledged": "Acknowledged. I will not call about this again today.",
        "not_understood": "Sorry, I did not get that.",
        "goodbye": "Goodbye.",
        "states": {"waiting": "waiting for input", "working": "working", "idle": "idle", "error": "in error"},
    },
    "de": {
        "greeting": "Hallo, hier ist dein Agent. Bitte gib deine PIN ein und drücke danach die Raute.",
        "pin_wrong": "Die PIN stimmt nicht. Bitte versuche es noch einmal.",
        "pin_locked": "Zu viele falsche Versuche. Auf Wiederhören.",
        "alert": "Meldung von {source}: {summary}",
        "menu": "Drücke 1 oder sage Status für deine Sessions. Drücke 2, um die Meldung zu wiederholen. "
        "Drücke 3, um sie zu bestätigen. Drücke 9 oder sage tschüss zum Auflegen.",
        "no_sessions": "Keine Session hat sich in letzter Zeit gemeldet.",
        "sessions_head": "{count} Sessions haben sich gemeldet.",
        "session_line": "{project} ist seit {age} Minuten {state}. {summary}",
        "more_sessions": "Und {count} weitere.",
        "acknowledged": "Bestätigt. Deswegen rufe ich heute nicht mehr an.",
        "not_understood": "Das habe ich nicht verstanden.",
        "goodbye": "Auf Wiederhören.",
        "states": {"waiting": "wartend", "working": "beschäftigt", "idle": "untätig", "error": "im Fehlerzustand"},
    },
}

KEYWORDS = {
    "status": ("status", "sessions", "session", "stand"),
    "repeat": ("repeat", "again", "wiederholen", "nochmal", "noch mal"),
    "ack": ("acknowledge", "ok", "okay", "bestätigen", "bestätigt", "erledigt"),
    "bye": ("goodbye", "bye", "hang up", "tschüss", "auflegen", "ende"),
}
DIGIT_COMMANDS = {"1": "status", "2": "repeat", "3": "ack", "9": "bye"}


@dataclass(frozen=True)
class Turn:
    """What to say, and which input to wait for. expect=None means hang up."""

    say: tuple[str, ...]
    expect: str | None  # "pin", "command" or None
    language: str = "en"


@dataclass
class Call:
    pin: str
    event_key: str
    event_summary: str
    event_source: str
    read_statuses: Callable[[], list[SessionStatus]]
    acknowledge: Callable[[str], None]
    language: str = "en"
    call_id: str = field(default_factory=lambda: secrets.token_urlsafe(16))
    created_at: float = field(default_factory=time.time)
    state: str = "new"
    pin_attempts: int = 0
    silent_turns: int = 0

    @property
    def t(self) -> dict:
        return TEXT[self.language]

    def _turn(self, *say: str, expect: str | None) -> Turn:
        if expect is None:
            self.state = "done"
        return Turn(tuple(say), expect, self.language)

    def start(self) -> Turn:
        self.state = "auth"
        return self._turn(self.t["greeting"], expect="pin")

    def handle(self, digits: str | None = None, speech: str | None = None) -> Turn:
        if self.state == "auth":
            return self._handle_pin(digits)
        if self.state == "menu":
            return self._handle_command(digits, speech)
        return self._turn(self.t["goodbye"], expect=None)

    def _alert(self) -> str:
        return self.t["alert"].format(source=self.event_source, summary=self.event_summary)

    def _handle_pin(self, digits: str | None) -> Turn:
        entered = (digits or "").strip().rstrip("#")
        if entered and hmac.compare_digest(entered.encode(), self.pin.encode()):
            self.state = "menu"
            return self._turn(self._alert(), self.t["menu"], expect="command")
        self.pin_attempts += 1
        if self.pin_attempts >= MAX_PIN_ATTEMPTS:
            return self._turn(self.t["pin_locked"], expect=None)
        return self._turn(self.t["pin_wrong"], expect="pin")

    def _parse_command(self, digits: str | None, speech: str | None) -> str | None:
        if digits:
            return DIGIT_COMMANDS.get(digits.strip()[:1])
        if speech:
            words = speech.lower()
            for command, keywords in KEYWORDS.items():
                if any(k in words for k in keywords):
                    return command
        return None

    def _handle_command(self, digits: str | None, speech: str | None) -> Turn:
        if not digits and not speech:
            self.silent_turns += 1
            if self.silent_turns > MAX_SILENT_TURNS:
                return self._turn(self.t["goodbye"], expect=None)
            return self._turn(self.t["menu"], expect="command")
        self.silent_turns = 0
        command = self._parse_command(digits, speech)
        if command == "status":
            return self._turn(*self._status_lines(), self.t["menu"], expect="command")
        if command == "repeat":
            return self._turn(self._alert(), self.t["menu"], expect="command")
        if command == "ack":
            self.acknowledge(self.event_key)
            return self._turn(self.t["acknowledged"], self.t["goodbye"], expect=None)
        if command == "bye":
            return self._turn(self.t["goodbye"], expect=None)
        return self._turn(self.t["not_understood"], self.t["menu"], expect="command")

    def _status_lines(self) -> list[str]:
        statuses = self.read_statuses()
        if not statuses:
            return [self.t["no_sessions"]]
        lines = [self.t["sessions_head"].format(count=len(statuses))]
        for status in statuses[:MAX_SESSIONS_READ]:
            lines.append(
                self.t["session_line"].format(
                    project=status.project,
                    state=self.t["states"][status.state],
                    age=status.age_minutes(),
                    summary=status.summary,
                ).strip()
            )
        if len(statuses) > MAX_SESSIONS_READ:
            lines.append(self.t["more_sessions"].format(count=len(statuses) - MAX_SESSIONS_READ))
        return lines


class CallRegistry:
    """Active calls by id. Calls expire so a leaked URL is useless later."""

    def __init__(self, ttl_s: int = 15 * 60) -> None:
        self.ttl_s = ttl_s
        self._calls: dict[str, Call] = {}
        self._lock = threading.Lock()

    def add(self, call: Call) -> None:
        with self._lock:
            self._prune()
            self._calls[call.call_id] = call

    def get(self, call_id: str | None) -> Call | None:
        if not call_id:
            return None
        with self._lock:
            self._prune()
            call = self._calls.get(call_id)
        if call is None or call.state == "done":
            return None
        return call

    def _prune(self) -> None:
        now = time.time()
        for call_id in [cid for cid, c in self._calls.items() if now - c.created_at > self.ttl_s]:
            del self._calls[call_id]
