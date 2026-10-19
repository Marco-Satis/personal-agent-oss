"""Approvals from the phone: a Discord webhook notice plus a one-time approval page.

Flow, when the chat runs with `--remote-approvals`:

1. The broker in the chat process hands a level-2/3 request to `RemoteChannel`.
2. `RemoteChannel` registers it with the running `serve` process over the local,
   bearer-protected `/internal/approvals` API (two processes cannot share the port).
3. `ApprovalInbox` in `serve` posts a short notice to the Discord webhook: tool name,
   level and a one-time link `<public base>/approve/<random id>`. No input content
   (notify.render_approval_notice has no parameter for it).
4. The person opens the link (served by `serve`, reachable only through the
   tailnet or proxy), sees the request and approves or denies with a CSRF-safe
   POST. Level 3 also needs the typed confirmation word and the approval PIN.
5. `RemoteChannel` polls the decision. Expiry, cancellation and every error mean deny.

Webhooks are write-only: nothing comes back through Discord. GET never changes
state, so link previews and prefetchers cannot approve anything.

With a paired Telegram bot (PA_NOTIFY_APPROVAL, by default Telegram when it is
ready), step 3 sends a Telegram message with inline buttons instead; each button
carries its own random one-time token and only the paired owner's presses count
(see telegram.py). Level 3 then needs the approval PIN as the next message. The
one-time page stays available as a review link when PA_PUBLIC_APPROVAL_URL is set.
"""

from __future__ import annotations

import hmac
import html
import json
import logging
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import __version__
from .approval import CONFIRM_WORD, CONTEXT_HINT, LEVEL_LABELS, ApprovalRequest, Level, approval_header
from .notify import DISCORD_MARKUP as _DISCORD_MARKUP  # noqa: F401 - kept for importers
from .notify import discord_safe, render_approval_notice  # noqa: F401 - discord_safe kept for importers
from .statefiles import read_private_json, remove_private, stat_key, write_private_json

log = logging.getLogger(__name__)

ID_RE = re.compile(r"[A-Za-z0-9_-]{32}")
EXPIRY_MARGIN_S = 5          # the page expires before the agent's own deadline
MAX_PENDING = 5
MAX_PIN_ATTEMPTS = 3         # wrong PINs before the request is denied
NOTICE_LIMIT = 5             # webhook posts ...
NOTICE_WINDOW_S = 10 * 60    # ... per window
PRUNE_AFTER_S = 10 * 60      # finished entries are kept this long after expiry
MAX_WAIT_S = 30
PIN_WAIT_S = 120             # Telegram level 3: the PIN must follow Approve within this time
MAX_PIN_LOCKS_PER_HOUR = 3   # more lockouts than this disable level-3 remote approvals for 24 h
MAX_WRONG_PINS_PER_DAY = 6   # this many wrong PINs in 24 h (any request, page or Telegram) disable them too
PIN_LOCK_WINDOW_S = 60 * 60
PIN_LOCK_KEEP_S = 24 * 60 * 60
PIN_LOCK_FILE = "approval-pinlock.json"
CALLBACK_TOKEN_BYTES = 16    # token_urlsafe(16): 22 characters, far below Telegram's 64-byte callback limit
CALLBACK_RE = re.compile(r"[A-Za-z0-9_-]{22}")
# The trusted origin labels a chat process may send with a console approval (broker scope labels).
ORIGIN_RE = re.compile(r"chat|task [0-9a-f]{6}( \([a-z0-9-]{1,32}\))?( · delegated)?( · telegram)?")
STATES = ("pending", "allowed", "denied", "expired")
FINAL_STATES = ("allowed", "denied", "expired")

TEXTS = {
    "en": {
        "title": "Approval needed",
        "level": "Level {level} ({label})",
        "expires": "Expires at {time}",
        "confirm": 'Type "{word}" to allow',
        "pin": "Approval PIN",
        "allow": "Allow",
        "deny": "Deny",
        "need_confirm": 'Type "{word}" to allow this level-3 action.',
        "bad_pin": "Wrong PIN. {left} attempt(s) left.",
        "approved": "Approved. You can close this page.",
        "denied": "Denied. You can close this page.",
        "locked": "Too many wrong PINs. The request was denied.",
        "invalid": "This approval link is unknown, expired or already used.",
        "rejected": "Request rejected.",
    },
    "de": {
        "title": "Freigabe nötig",
        "level": "Stufe {level} ({label})",
        "expires": "Läuft ab um {time}",
        "confirm": '"{word}" eingeben, um zu erlauben',
        "pin": "Freigabe-PIN",
        "allow": "Erlauben",
        "deny": "Ablehnen",
        "need_confirm": 'Für Stufe 3 bitte "{word}" eingeben.',
        "bad_pin": "Falsche PIN. Noch {left} Versuch(e).",
        "approved": "Erlaubt. Die Seite kann geschlossen werden.",
        "denied": "Abgelehnt. Die Seite kann geschlossen werden.",
        "locked": "Zu viele falsche PINs. Die Anfrage wurde abgelehnt.",
        "invalid": "Dieser Freigabe-Link ist unbekannt, abgelaufen oder schon benutzt.",
        "rejected": "Anfrage abgewiesen.",
    },
}


def _texts(language: str) -> dict[str, str]:
    return TEXTS.get(language, TEXTS["en"])


def _close_quietly(exc: urllib.error.HTTPError) -> None:
    try:
        exc.close()
    except Exception:  # noqa: BLE001 - an HTTPError without a body cannot be closed
        pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects become errors: a secret URL or bearer token never follows one."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def local_opener() -> Callable:
    """Opener for calls to our own server: never through a proxy, never redirected.

    With HTTP_PROXY set and no_proxy unset, urllib would send 127.0.0.1 requests,
    bearer token included, to the proxy.
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect()).open


# --- Discord -------------------------------------------------------------------


class DiscordWebhook:
    """Posts a plain notice to a Discord webhook. The URL is a secret and never logged."""

    def __init__(self, url: str, timeout_s: int = 10, opener: Callable | None = None) -> None:
        self._url = url
        self.timeout_s = timeout_s
        self._opener = opener or urllib.request.build_opener(_NoRedirect()).open

    def __repr__(self) -> str:
        return "DiscordWebhook(<hidden>)"

    def post(self, text: str) -> bool:
        body = json.dumps(
            {"content": text[:1900], "allowed_mentions": {"parse": []}, "flags": 4}  # 4: no embeds
        ).encode()
        try:
            request = urllib.request.Request(self._url, data=body, method="POST")
            request.add_header("Content-Type", "application/json")
            request.add_header("User-Agent", f"personal-agent/{__version__}")
            with self._opener(request, timeout=self.timeout_s) as response:
                code = getattr(response, "status", None) or response.getcode()
        except urllib.error.HTTPError as exc:
            _close_quietly(exc)
            log.warning("webhook post failed: HTTPError %s", exc.code)
            return False
        except Exception as exc:  # noqa: BLE001 - messages of some errors contain the URL
            log.warning("webhook post failed: %s", type(exc).__name__)
            return False
        if not 200 <= int(code) < 300:
            log.warning("webhook post failed: HTTP %s", code)
            return False
        return True


# --- the inbox in the server process -------------------------------------------


class PinLockout:
    """Wrong approval PINs and PIN lockouts, counted across the page and Telegram: one
    budget for an attacker, not two.

    Every wrong PIN counts, not only the third one on a request: otherwise two wrong
    guesses followed by Deny (or by letting the PIN wait run out) would never add up.
    MAX_WRONG_PINS_PER_DAY wrong PINs within 24 hours, or more than
    MAX_PIN_LOCKS_PER_HOUR lockouts within an hour, disable level-3 remote approvals for
    24 hours. With a state directory the record survives a restart (a crash loop under
    systemd must not reset the brute-force protection), and every process re-reads the
    file when it changed, so `personal-agent telegram status --clear-lock` in a terminal
    also re-enables a running serve.
    """

    def __init__(self, state_dir: Path | None, clock: Callable[[], float] = time.time) -> None:
        self.state_dir = Path(state_dir) if state_dir is not None else None
        self.clock = clock
        self._lock = threading.Lock()
        self._locks: list[float] = []
        self._wrong: list[float] = []
        self._disabled_at: float | None = None
        self._key: tuple[int, int, int] | None = None
        self._refresh()

    @staticmethod
    def _times(value: object) -> list[float]:
        if not isinstance(value, list):
            return []
        return [float(t) for t in value[-50:] if isinstance(t, (int, float)) and not isinstance(t, bool)]

    def _refresh(self) -> None:
        """Re-read the file when it changed on disk (another process wrote or cleared it). Under the lock."""
        if self.state_dir is None:
            return
        key = stat_key(self.state_dir, PIN_LOCK_FILE)
        if key == self._key:
            return
        self._key = key
        self._locks, self._wrong, self._disabled_at = [], [], None
        if key is None:
            return  # removed: cleared from the terminal
        data = read_private_json(self.state_dir, PIN_LOCK_FILE, 4096, 1)
        if data is None:
            return
        self._locks = self._times(data.get("locks"))
        self._wrong = self._times(data.get("wrong"))
        disabled = data.get("disabled_at")
        if isinstance(disabled, (int, float)) and not isinstance(disabled, bool):
            self._disabled_at = float(disabled)

    def _save(self) -> None:
        if self.state_dir is None:
            return
        write_private_json(self.state_dir, PIN_LOCK_FILE, {
            "version": 1, "locks": self._locks, "wrong": self._wrong, "disabled_at": self._disabled_at})
        self._key = stat_key(self.state_dir, PIN_LOCK_FILE)

    def record_wrong(self, lockout: bool = False) -> bool:
        """Count one wrong PIN (lockout: it was the last attempt on its request).

        True when level-3 remote approvals are now disabled.
        """
        with self._lock:
            self._refresh()
            now = self.clock()
            self._wrong = [t for t in self._wrong if now - t < PIN_LOCK_KEEP_S][-49:] + [now]
            if lockout:
                self._locks = [t for t in self._locks if now - t < PIN_LOCK_KEEP_S][-49:] + [now]
            recent_locks = sum(1 for t in self._locks if now - t < PIN_LOCK_WINDOW_S)
            if self._disabled_at is None and (len(self._wrong) >= MAX_WRONG_PINS_PER_DAY
                                              or recent_locks > MAX_PIN_LOCKS_PER_HOUR):
                self._disabled_at = now
                log.warning("level-3 remote approvals disabled: too many wrong PINs")
            self._save()
            return self._disabled_now(now)

    def record(self) -> bool:
        """Count one lockout (the last wrong PIN of a request)."""
        return self.record_wrong(lockout=True)

    def _disabled_now(self, now: float) -> bool:
        return self._disabled_at is not None and now - self._disabled_at < PIN_LOCK_KEEP_S

    def disabled(self) -> bool:
        with self._lock:
            self._refresh()
            return self._disabled_now(self.clock())

    def status(self) -> tuple[int, bool]:
        """(wrong PINs in the last 24 h, disabled)."""
        with self._lock:
            self._refresh()
            now = self.clock()
            return sum(1 for t in self._wrong if now - t < PIN_LOCK_KEEP_S), self._disabled_now(now)

    def clear(self) -> None:
        with self._lock:
            self._locks, self._wrong = [], []
            self._disabled_at = None
            if self.state_dir is not None:
                remove_private(self.state_dir, PIN_LOCK_FILE)
                self._key = None


@dataclass
class PendingApproval:
    id: str
    tool_name: str
    level: int
    summary: str
    expires_at: float
    csrf: str
    state: str = "pending"          # pending | allowed | denied | expired
    pin_attempts: int = 0
    done: threading.Event = field(default_factory=threading.Event)
    via: str = "discord"            # discord: notice plus page; telegram: buttons (the page works too)
    origin: str = "chat"            # trusted label: chat, telegram, task 3f2a9c (research) ...
    context: bool = False           # the request may come from note text or a plugin skill
    sequence: int = 0               # per-serve number shown in Telegram prompts ("Approval #7")
    approve_token: str | None = field(default=None, repr=False)  # callback data: pure randomness
    deny_token: str | None = field(default=None, repr=False)
    stop_token: str | None = field(default=None, repr=False)     # "Deny and stop turn" (Telegram turns)
    chat_id: int | None = None      # where the Telegram prompt was sent
    message_id: int | None = None
    prompt_text: str | None = field(default=None, repr=False)    # the prompt without the review link
    pin_until: float | None = None  # level 3: Approve was pressed, the PIN is expected until then
    pin_message_id: int | None = None
    stop_requested: bool = False


class ApprovalInbox:
    """Pending remote approvals, each with a one-time id and a CSRF token.

    One inbox, two delivery paths: a Discord notice plus the one-time page, or a Telegram
    message with inline buttons (whose callback data are separate random tokens). Expiry,
    single use, PIN attempts, MAX_PENDING and the notice rate limit are the same for both;
    NOTICE_LIMIT counts Telegram turns and console approvals together and fails closed.
    """

    def __init__(
        self,
        notifier: DiscordWebhook | None,
        public_base: str | None,
        language: str = "en",
        max_timeout_s: int = 300,
        pin: str | None = None,
        clock: Callable[[], float] = time.time,
        *,
        route: Callable[[], str | None] | None = None,
        telegram: object | None = None,
        on_final: Callable[[PendingApproval], None] | None = None,
        state_dir: Path | None = None,
    ) -> None:
        self.notifier = notifier
        self.public_base = public_base.rstrip("/") if public_base else None
        # Which channel delivers a console approval (PA_NOTIFY_APPROVAL, see notify.resolve_route):
        # "telegram", "discord", or None when no approval channel is ready. Default: Discord.
        self._route = route
        self.telegram = telegram  # telegram.TelegramApprovalSender: send(entry), close(entry)
        self.on_final = on_final  # called outside the lock for every entry that reached a final state
        self.language = language
        self.max_timeout_s = max_timeout_s
        self._pin = pin
        self.clock = clock
        self.lockout = PinLockout(state_dir, clock)
        self._lock = threading.Lock()
        self._entries: dict[str, PendingApproval] = {}
        self._notices: list[float] = []
        self._finals: list[PendingApproval] = []
        self._sequence = 0

    def __repr__(self) -> str:
        return f"ApprovalInbox({len(self._entries)} entries)"

    # --- internal, under the lock ---

    def _expire(self, entry: PendingApproval, now: float) -> None:
        if entry.state == "pending" and now >= entry.expires_at:
            self._finish(entry, "expired")

    def _finish(self, entry: PendingApproval, state: str) -> None:
        entry.state = state
        entry.done.set()
        self._finals.append(entry)
        log.info("approval %s level %s -> %s", entry.id[:6], entry.level, state)

    def _prune(self, now: float) -> None:
        for entry in list(self._entries.values()):
            self._expire(entry, now)
            if now > entry.expires_at + PRUNE_AFTER_S:
                del self._entries[entry.id]

    # --- outside the lock ---

    def _drain(self) -> None:
        """Tell on_final about entries that reached a final state (e.g. to close a Telegram prompt)."""
        with self._lock:
            finals, self._finals = self._finals, []
        callback = self.on_final
        if callback is None:
            return
        for entry in finals:
            try:
                callback(entry)
            except Exception as exc:  # noqa: BLE001 - closing a prompt is best effort
                log.warning("approval close hook failed: %s", type(exc).__name__)

    def _deliver_telegram(self, entry: PendingApproval) -> bool:
        if self.telegram is None:
            return False
        try:
            placed = self.telegram.send(entry)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - a failed prompt is a deny
            log.warning("telegram approval prompt failed: %s", type(exc).__name__)
            return False
        if not placed:
            return False
        with self._lock:
            entry.chat_id, entry.message_id = int(placed[0]), int(placed[1])
            if entry.state != "pending":
                self._finals.append(entry)  # decided while the prompt was sent: close it now
        return True

    # --- API ---

    def register(self, tool_name: str, level: int, summary: str, timeout_s: int, *, via: str | None = None,
                 origin: str = "chat", context: bool = False, stoppable: bool = False) -> PendingApproval | None:
        """Create a pending approval and send the notice or prompt. None means: deny.

        via None means the configured route (console approvals). Telegram-origin turns pass
        via="telegram" and stoppable=True (a third button denies and stops the turn).
        """
        now = self.clock()
        lifetime = min(timeout_s, self.max_timeout_s) - EXPIRY_MARGIN_S
        if level not in (Level.WRITE, Level.DANGER) or lifetime <= 0:
            return None
        if level == Level.DANGER and not self._pin:
            log.warning("level-3 remote approval refused: PA_APPROVAL_PIN is not set")
            return None
        if level == Level.DANGER and self.lockout.disabled():
            log.warning("level-3 remote approval refused: disabled after too many PIN lockouts")
            return None
        if via is None:
            via = "discord" if self._route is None else self._route()
        if via == "discord":
            available = self.notifier is not None and bool(self.public_base)
        elif via == "telegram":
            available = self.telegram is not None
        else:
            available = False
        if not available:
            log.warning("remote approval refused: no approval channel available (%s)", via or "none")
            return None
        with self._lock:
            self._prune(now)
            if sum(1 for e in self._entries.values() if e.state == "pending") >= MAX_PENDING:
                log.warning("remote approval refused: too many pending")
                return None
            self._notices = [t for t in self._notices if now - t < NOTICE_WINDOW_S]
            if len(self._notices) >= NOTICE_LIMIT:
                log.warning("remote approval refused: notice rate limit reached")
                return None
            self._notices.append(now)
            self._sequence += 1
            entry = PendingApproval(
                id=secrets.token_urlsafe(24),
                tool_name=tool_name[:200],  # shown HTML-escaped; the broker already ASCII-escaped it
                level=int(level),
                summary=summary,
                expires_at=now + lifetime,
                csrf=secrets.token_urlsafe(24),
                via=via,
                origin=origin,
                context=bool(context),
                sequence=self._sequence,
            )
            if via == "telegram":
                entry.approve_token = secrets.token_urlsafe(CALLBACK_TOKEN_BYTES)
                entry.deny_token = secrets.token_urlsafe(CALLBACK_TOKEN_BYTES)
                if stoppable:
                    entry.stop_token = secrets.token_urlsafe(CALLBACK_TOKEN_BYTES)
            self._entries[entry.id] = entry
        # The post may take seconds: never hold the lock over the network.
        if via == "discord":
            assert self.notifier is not None
            notice = render_approval_notice(
                "discord", tool_name, entry.level, "chat", False,
                minutes=max(1, round(lifetime / 60)),
                link=f"{self.public_base}/approve/{entry.id}",
                language=self.language,
            )
            sent = self.notifier.post(notice)
        else:
            sent = self._deliver_telegram(entry)
        if not sent:
            with self._lock:
                self._entries.pop(entry.id, None)
                if entry.state == "pending":
                    self._finish(entry, "denied")
            self._drain()
            return None
        log.info("approval %s level %s pending, %s sent", entry.id[:6], entry.level,
                 "notice" if via == "discord" else "prompt")
        self._drain()
        return entry

    def review_link(self, entry: PendingApproval) -> str | None:
        """The one-time page link (a bearer capability), when PA_PUBLIC_APPROVAL_URL is set."""
        return f"{self.public_base}/approve/{entry.id}" if self.public_base else None

    def get(self, approval_id: str) -> PendingApproval | None:
        """The entry while it can still be decided, else None."""
        with self._lock:
            entry = self._entries.get(approval_id)
            if entry is not None:
                self._expire(entry, self.clock())
            result = entry if entry is not None and entry.state == "pending" else None
        self._drain()
        return result

    def resolve(self, approval_id: str, csrf: str, decision: str, confirm: str = "", pin: str = "") -> str:
        """ok | invalid | bad_csrf | bad_request | need_confirm | bad_pin | locked."""
        with self._lock:
            outcome = self._resolve(approval_id, csrf, decision, confirm, pin)
        outcome = self._count_wrong_pin(approval_id, outcome)
        self._drain()
        return outcome

    def _count_wrong_pin(self, approval_id: str, outcome: str) -> str:
        """Every wrong PIN counts toward the shared lockout (outside the inbox lock: it writes a file).

        When this wrong PIN disables level-3 remote approvals, the request is denied at once.
        """
        if outcome not in ("bad_pin", "locked"):
            return outcome
        disabled = self.lockout.record_wrong(lockout=outcome == "locked")
        if outcome == "bad_pin" and disabled:
            with self._lock:
                entry = self._entries.get(approval_id)
                if entry is not None and entry.state == "pending":
                    entry.pin_until = None
                    self._finish(entry, "denied")
            return "locked"
        return outcome

    def _resolve(self, approval_id: str, csrf: str, decision: str, confirm: str, pin: str) -> str:
        entry = self._entries.get(approval_id)
        if entry is None:
            return "invalid"
        self._expire(entry, self.clock())
        if entry.state != "pending":
            return "invalid"
        if not hmac.compare_digest(csrf.encode("utf-8"), entry.csrf.encode("utf-8")):
            return "bad_csrf"
        if decision == "deny":
            self._finish(entry, "denied")
            return "ok"
        if decision != "allow":
            return "bad_request"
        if entry.level >= Level.DANGER:
            if confirm.strip().lower() != CONFIRM_WORD:
                return "need_confirm"
            if self.lockout.disabled():
                self._finish(entry, "denied")
                return "invalid"
            expected = self._pin or ""
            if not expected or not hmac.compare_digest(pin.strip().encode("utf-8"), expected.encode("utf-8")):
                entry.pin_attempts += 1
                log.warning("approval %s: wrong PIN", entry.id[:6])
                if entry.pin_attempts >= MAX_PIN_ATTEMPTS:
                    self._finish(entry, "denied")
                    return "locked"
                return "bad_pin"
        self._finish(entry, "allowed")
        return "ok"

    def _by_token(self, token: str) -> tuple[PendingApproval | None, str]:
        """The Telegram entry and which button ("approve", "deny", "stop"). Every token is compared."""
        found: tuple[PendingApproval | None, str] = (None, "")
        probe = token.encode("ascii")
        for entry in self._entries.values():
            if entry.via != "telegram":
                continue
            for kind, value in (("approve", entry.approve_token), ("deny", entry.deny_token),
                                ("stop", entry.stop_token)):
                if value and hmac.compare_digest(probe, value.encode("ascii")):
                    found = (entry, kind)
        return found

    def press(self, token: str, chat_id: int, message_id: int) -> str:
        """A button press on a Telegram prompt (callers already checked it is the paired owner).

        allowed | denied | stopped | need_pin | pin_busy | pin_disabled | invalid. The token
        must belong to a pending Telegram entry and the press must come from that prompt
        message. Level 3: Approve starts a PIN wait; the decision follows from submit_pin.
        """
        if not isinstance(token, str) or not CALLBACK_RE.fullmatch(token):
            return "invalid"
        with self._lock:
            outcome = self._press(token, chat_id, message_id)
        self._drain()
        return outcome

    def _press(self, token: str, chat_id: int, message_id: int) -> str:
        now = self.clock()
        entry, kind = self._by_token(token)
        if entry is None:
            return "invalid"
        self._expire(entry, now)
        if entry.state != "pending" or entry.chat_id != chat_id or entry.message_id != message_id:
            return "invalid"
        if kind == "deny":
            self._finish(entry, "denied")
            return "denied"
        if kind == "stop":
            entry.stop_requested = True
            self._finish(entry, "denied")
            return "stopped"
        if entry.level < Level.DANGER:
            self._finish(entry, "allowed")
            return "allowed"
        if self.lockout.disabled():
            return "pin_disabled"
        for other in self._entries.values():
            if (other is not entry and other.state == "pending" and other.pin_until is not None
                    and other.pin_until > now):
                return "pin_busy"
        if entry.pin_until is not None and entry.pin_until > now:
            return "need_pin"  # pressing Approve again never extends a running PIN wait
        entry.pin_until = min(entry.expires_at, now + PIN_WAIT_S)
        return "need_pin"

    def pin_entry(self) -> PendingApproval | None:
        """The one entry whose PIN is awaited right now. Expired PIN waits end here (lazily)."""
        with self._lock:
            now = self.clock()
            found = None
            for entry in self._entries.values():
                self._expire(entry, now)
                if entry.pin_until is None:
                    continue
                if entry.state != "pending" or entry.pin_until <= now:
                    entry.pin_until = None  # the person may press Approve again
                    continue
                found = entry
        self._drain()
        return found

    def submit_pin(self, approval_id: str, pin: str) -> str:
        """ok | bad_pin | locked | invalid. MAX_PIN_ATTEMPTS per entry, shared with the page."""
        wrong = False
        with self._lock:
            entry = self._entries.get(approval_id)
            now = self.clock()
            if entry is not None:
                self._expire(entry, now)
            if (entry is None or entry.state != "pending" or entry.pin_until is None
                    or entry.pin_until <= now):
                outcome = "invalid"
            elif self.lockout.disabled():
                self._finish(entry, "denied")
                outcome = "locked"
            elif self._pin and hmac.compare_digest(pin.strip().encode("utf-8"), self._pin.encode("utf-8")):
                entry.pin_until = None
                self._finish(entry, "allowed")
                outcome = "ok"
            else:
                wrong = True
                entry.pin_attempts += 1
                log.warning("approval %s: wrong PIN", entry.id[:6])
                if entry.pin_attempts >= MAX_PIN_ATTEMPTS:
                    entry.pin_until = None
                    self._finish(entry, "denied")
                    outcome = "locked"
                else:
                    outcome = "bad_pin"
        if wrong:
            outcome = self._count_wrong_pin(approval_id, outcome)
        self._drain()
        return outcome

    def sweep(self) -> None:
        """Expire and prune; this is how expired Telegram prompts get closed."""
        with self._lock:
            self._prune(self.clock())
        self._drain()

    def cancel(self, approval_id: str) -> bool:
        """The agent stopped waiting: a later click must not show "approved"."""
        with self._lock:
            entry = self._entries.get(approval_id)
            if entry is not None and entry.state == "pending":
                self._finish(entry, "expired")
        self._drain()
        return entry is not None

    def deny_telegram(self, chat_id: int | None = None) -> int:
        """Deny every pending Telegram entry (of one chat, if given): unpairing, a new owner or
        a suspected token leak. Returns how many were denied."""
        count = 0
        with self._lock:
            for entry in self._entries.values():
                if entry.via == "telegram" and entry.state == "pending" and (
                        chat_id is None or entry.chat_id is None or entry.chat_id == chat_id):
                    self._finish(entry, "denied")
                    count += 1
        self._drain()
        return count

    def cancel_all(self) -> None:
        """Shutdown: every pending entry expires (a later click cannot approve anything)."""
        with self._lock:
            for entry in self._entries.values():
                if entry.state == "pending":
                    self._finish(entry, "expired")
        self._drain()

    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for e in self._entries.values() if e.state == "pending")

    def wait(self, approval_id: str, timeout_s: float) -> str | None:
        """The state after at most timeout_s (capped at 30 s); None for an unknown id."""
        end = time.monotonic() + max(0.0, min(timeout_s, MAX_WAIT_S))
        while True:
            with self._lock:
                entry = self._entries.get(approval_id)
                if entry is not None:
                    self._expire(entry, self.clock())
                state = entry.state if entry is not None else None
            self._drain()
            if entry is None:
                return None
            if state != "pending":
                return state
            remaining = end - time.monotonic()
            if remaining <= 0:
                return "pending"
            entry.done.wait(min(remaining, 1.0))

    def pin_attempts_left(self, entry: PendingApproval) -> int:
        return max(0, MAX_PIN_ATTEMPTS - entry.pin_attempts)


# --- pages ---------------------------------------------------------------------

_STYLE = (
    "body{font-family:system-ui,sans-serif;max-width:36rem;margin:1.5rem auto;padding:0 1rem;"
    "line-height:1.4;color:#1a1a1a;background:#fafafa}"
    "@media (prefers-color-scheme:dark){body{color:#eee;background:#161616}}"
    "pre{white-space:pre-wrap;word-break:break-all;padding:.6rem;border:1px solid #8886}"
    "input,button{font-size:1.05rem;padding:.5rem;margin:.25rem 0}"
    "button{min-width:8rem;margin-right:.5rem}.err{color:#c62828;font-weight:600}"
)


def _page(title: str, body: str, language: str) -> str:
    return (
        f'<!doctype html><html lang="{html.escape(language)}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body>{body}</body></html>"
    )


def render_page(p: PendingApproval, language: str = "en", error: str | None = None) -> str:
    """The approval form. No JavaScript; every dynamic value is escaped."""
    t = _texts(language)
    level = Level(p.level)
    expires = time.strftime("%H:%M:%S", time.localtime(p.expires_at))
    parts = [
        f"<h1>{html.escape(t['title'])}</h1>",
        f"<p><strong>{html.escape(p.tool_name)}</strong> &middot; "
        f"{html.escape(t['level'].format(level=int(level), label=LEVEL_LABELS[level]))}</p>",
        f"<pre>{html.escape(p.summary)}</pre>",
        f"<p>{html.escape(t['expires'].format(time=expires))}</p>",
    ]
    if error:
        parts.append(f'<p class="err">{html.escape(error)}</p>')
    form = [
        '<form method="post">',
        f'<input type="hidden" name="csrf" value="{html.escape(p.csrf, quote=True)}">',
    ]
    if level >= Level.DANGER:
        form += [
            f'<p><label>{html.escape(t["confirm"].format(word=CONFIRM_WORD))}<br>'
            '<input name="confirm" autocomplete="off" autocapitalize="none" spellcheck="false"></label></p>',
            f'<p><label>{html.escape(t["pin"])}<br>'
            '<input name="pin" type="password" inputmode="numeric" autocomplete="off"></label></p>',
        ]
    form += [
        f'<p><button name="decision" value="allow">{html.escape(t["allow"])}</button>'
        f'<button name="decision" value="deny">{html.escape(t["deny"])}</button></p>',
        "</form>",
    ]
    return _page(t["title"], "".join(parts + form), language)


def render_message(text: str, language: str = "en") -> str:
    """A page for a final, invalid or rejected state."""
    return _page(_texts(language)["title"], f"<p>{html.escape(text)}</p>", language)


# --- the channel in the chat process -------------------------------------------


class RemoteChannel:
    """Approval channel that asks through the running `serve` process."""

    def __init__(
        self,
        base_url: str,
        api_token: str,
        lock: threading.Lock,
        write: Callable[[str], None] = print,
        opener: Callable | None = None,
        poll_s: int = 2,  # short polls: a stop is noticed (and cancelled) within the broker's close join
        stop: Callable[[], bool] | None = None,
        stop_for: Callable[[ApprovalRequest], bool] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = api_token
        self.lock = lock
        self.write = write
        self._opener = opener or local_opener()
        self.poll_s = poll_s
        self._stop = stop or (lambda: False)
        self._stop_for = stop_for

    def _stopped(self, request: ApprovalRequest) -> bool:
        return self._stop() or (self._stop_for is not None and self._stop_for(request))

    @staticmethod
    def page_summary(request: ApprovalRequest) -> str:
        """What the approval page shows: who asks first, then the context hint and the request."""
        lines = [approval_header(request)]
        if request.origin:
            lines.append(f"task: {request.origin}")
        if request.context:
            lines.append(f"({request.context}: {CONTEXT_HINT})")
        lines.append(request.summary)
        return "\n".join(lines)

    def __repr__(self) -> str:
        return f"RemoteChannel({self.base_url!r})"

    def _note(self, line: str) -> None:
        if self.lock.acquire(timeout=5):
            try:
                self.write(line)
            finally:
                self.lock.release()

    def _request(self, method: str, path: str, payload: dict | None, timeout: float) -> tuple[int, dict]:
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        request.add_unredirected_header("Authorization", f"Bearer {self._token}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self._opener(request, timeout=timeout) as response:
                status, body = response.status, response.read(4096)
        except urllib.error.HTTPError as exc:
            status, body = exc.code, b""
            _close_quietly(exc)
        reply = json.loads(body) if body else {}
        return status, reply if isinstance(reply, dict) else {}

    def ask(self, request: ApprovalRequest, deadline: float) -> bool:
        if request.full is not None:
            self._note(f"[approval for {request.tool_name} is too large to review remotely: denied]")
            return False
        remaining = int(deadline - time.monotonic())
        if remaining <= EXPIRY_MARGIN_S:
            return False
        summary = self.page_summary(request)
        if len(summary) > 2000:
            self._note(f"[approval for {request.tool_name} is too large to review remotely: denied]")
            return False
        approval_id: str | None = None
        state = "pending"
        try:
            status, reply = self._request(
                "POST",
                "/internal/approvals",
                {"tool_name": request.tool_name, "level": int(request.level),
                 "summary": summary,
                 "timeout_s": remaining,
                 # Trusted labels only: who asks (the broker's scope label) and whether
                 # note text or a plugin skill may stand behind the request.
                 "origin": request.scope_label,
                 "context": bool(request.context)},
                # The server posts the notice before it answers. Wait as long as the entry
                # can live: if this call gives up, the entry it may have created has expired.
                timeout=remaining,
            )
            if status == 503:
                self._note("[remote approvals are not configured on the server: denied]")
                return False
            candidate = reply.get("id")
            if status != 200 or not isinstance(candidate, str) or not ID_RE.fullmatch(candidate):
                self._note(f"[remote approval not available (HTTP {status}): denied]")
                return False
            approval_id = candidate
            sent = "buttons sent to Telegram" if reply.get("via") == "telegram" else "link sent"
            self._note(f"[approval pending remotely · {request.scope_label}: {request.tool_name} "
                       f"(level {int(request.level)}), {sent}]")
            while state == "pending":
                if self._stopped(request):
                    break
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                wait = max(0, min(self.poll_s, int(left)))
                status, reply = self._request(
                    "GET", f"/internal/approvals/{approval_id}?wait={wait}", None, timeout=wait + 10
                )
                new_state = reply.get("state")
                if status != 200 or new_state not in STATES:
                    state = "unknown"
                    break
                state = new_state
                if state == "pending" and wait == 0:
                    time.sleep(min(0.2, max(left, 0)))
        except Exception as exc:  # noqa: BLE001 - every failure is a deny
            self._note(f"[remote approval failed ({type(exc).__name__}): denied]")
            state = "error"
        finally:
            # Cancel on every path that did not end in a final state (deadline, stop, poll
            # error, bad reply), so a later click can never show "approved" for a denial.
            if approval_id is not None and state not in FINAL_STATES:
                try:
                    self._request("POST", f"/internal/approvals/{approval_id}/cancel", {}, timeout=3)
                except Exception:  # noqa: BLE001 - best effort; the entry expires anyway
                    pass
        if state == "allowed":
            self._note("[approved remotely]")
            return True
        if state in ("pending", "expired"):
            self._note("[remote approval timed out: denied]")
        elif state == "denied":
            self._note("[denied remotely]")
        return False
