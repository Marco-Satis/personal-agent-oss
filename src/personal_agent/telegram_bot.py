"""The Telegram assistant inside `serve`: poller, inbound chat, approval buttons, outbox.

Threads (one "generation" per pairing, started and stopped by the supervisor):

  telegram-supervisor  every 5 s: sweep expired approvals, notice pairing, unpairing,
                       an owner change, a rejected token or a hijacked bot
  telegram-poller      getUpdates long poll; button presses are decided inline,
                       owner text is checked (PIN, rate, commands) and queued
  telegram-turns       one turn at a time through the shared assistant (Router)
  telegram-outbox      fire-and-forget sends and edits (busy replies, prompt closes)

The turn and the poller are separate threads on purpose: during a turn the approval
is decided by a button press that only the poller receives. The worker blocks in
router.handle, the broker's handler thread blocks in TelegramChannel.ask, and the
poller resolves the inbox entry; no thread waits on another in a cycle.

Inbound text is untrusted in the sense that it could come from a compromised
Telegram account: it is never more trusted than the console. Every approval stays,
level 3 needs the PIN, `quota reset` is refused, session lists stay local, there is
no delegation, and tasks only queue (they run with the console's approvals).
"""

from __future__ import annotations

import hmac
import logging
import queue
import random
import re
import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Callable

from .notify import info_notice, split_message, telegram_untrusted, utf16_len
from .remote import EXPIRY_MARGIN_S, MAX_WAIT_S, ApprovalInbox, PendingApproval, RemoteChannel
from .status import clean_text
from .telegram import (
    BACKOFF_CAP_S,
    POLL_TIMEOUT_S,
    SAFE_TEXT_LIMIT,
    UPDATES_LIMIT,
    OffsetStore,
    Owner,
    OwnerStore,
    PollLock,
    TelegramApprovalSender,
    TelegramClient,
    TelegramError,
    TelegramTarget,
)
from .textsafe import strip_invisible

if TYPE_CHECKING:
    from .approval import ApprovalBroker, ApprovalRequest
    from .assistant import AssistantRuntime
    from .config import Config
    from .notify import Notifier

log = logging.getLogger(__name__)

MAX_QUEUED_TURNS = 3
MAX_OWNER_MESSAGES_PER_MIN = 20
STALE_MESSAGE_S = 300
IDLE_CLOSE_S = 1800
CONFLICT_WAIT_S = 30
# A 409 while this process holds telegram.lock means a foreign poller (a leaked token): Telegram ends
# the older of two getUpdates requests, and our own abandoned long poll belongs to a stopped
# generation, whose conflicts are ignored. So the first conflict counts, and successful polls in
# between never reset the count (an attacker leaving gaps must not escape detection).
CONFLICTS_FOR_HIJACK = 1
STRANGER_LOG_EVERY_S = 600
SUPERVISOR_TICK_S = 5
REPLY_DEADLINE_S = 60
UNAVAILABLE_RETRY_S = 60
CALLBACK_SEEN_S = 600
PIN_LIKE = re.compile(r"[0-9]{6,32}")
CALLBACK_DATA = re.compile(r"[A-Za-z0-9_-]{22}")
REPLY_CUT_NOTE = "\n… (reply cut: too long for Telegram)"
FORWARD_FIELDS = ("forward_origin", "forward_from", "forward_from_chat", "forward_date", "via_bot",
                  "external_reply")
FOREIGN_FIELDS = ("business_connection_id", "guest_query_id", "sender_business_bot")

TEXTS = {
    "en": {
        "stale": "Not processed: sent while the agent was offline. Send it again.",
        "slow": "Slow down: too many messages. Some were not processed.",
        "pin_ok": "Approved.",
        "pin_bad": "Wrong PIN, {n} attempt(s) left.",
        "pin_locked": "Denied: too many wrong PINs.",
        "pin_invalid": "This approval is no longer open.",
        "pin_like": "That looks like your PIN; it was not sent to the assistant.",
        "pin_waiting": "Waiting for your PIN; message not sent.",
        "pin_prompt": "PIN for level-3 approval of {tool}: your next message is used as the PIN (2 min).",
        "no_text": "Only text messages are supported.",
        "heard": "Heard: {text}",
        "voice_failed": "Could not understand the voice message ({reason}). Please type it.",
        "voice_long": "Voice messages up to {n} seconds only.",
        "forwarded": "Forwarded messages are not processed; copy the text if you mean it.",
        "new": "Started a new conversation.",
        "terminal_only": "Only in the terminal.",
        "not_here": "Not available over Telegram.",
        "locked": "Telegram is locked: this chat no longer controls personal-agent. Pair again in a terminal.",
        "inbound_off": "Chat over Telegram is off (PA_TELEGRAM_INBOUND).",
        "busy": "Busy: a turn is running and {n} messages are waiting. Send it again later.",
        "queued": "Queued ({n} ahead).",
        "unavailable": "The assistant is unavailable: {reason}",
        "turn_limit": "Not sent: the hourly limit of model turns over Telegram is reached "
                      "(PA_TELEGRAM_MAX_TURNS_PER_HOUR). Commands like note, tasks and help still work.",
        "error": "[error] the turn failed ({reason})",
        "approvals": "Approvals: buttons under each request; level 3 also needs your approval PIN as the next message.",
        "cb_allowed": "Approved",
        "cb_denied": "Denied",
        "cb_stopped": "Denied; the turn is stopped",
        "cb_need_pin": "Send your approval PIN as your next message (2 min)",
        "cb_pin_busy": "Finish the other PIN request first",
        "cb_pin_disabled": "Level-3 approvals over Telegram are disabled for now",
        "cb_invalid": "Expired or already decided",
        "too_large": "[approval for {tool} is too large to review remotely: denied]",
    },
    "de": {
        "stale": "Nicht verarbeitet: gesendet, während der Agent offline war. Bitte erneut senden.",
        "slow": "Langsamer bitte: zu viele Nachrichten. Einige wurden nicht verarbeitet.",
        "pin_ok": "Erlaubt.",
        "pin_bad": "Falsche PIN, noch {n} Versuch(e).",
        "pin_locked": "Abgelehnt: zu viele falsche PINs.",
        "pin_invalid": "Diese Freigabe ist nicht mehr offen.",
        "pin_like": "Das sieht aus wie deine PIN; sie wurde nicht an den Assistenten gesendet.",
        "pin_waiting": "Warte auf deine PIN; Nachricht nicht gesendet.",
        "pin_prompt": "PIN für die Stufe-3-Freigabe von {tool}: deine nächste Nachricht gilt als PIN (2 Min.).",
        "no_text": "Nur Textnachrichten werden unterstützt.",
        "heard": "Verstanden: {text}",
        "voice_failed": "Sprachnachricht nicht verstanden ({reason}). Bitte tippen.",
        "voice_long": "Sprachnachrichten nur bis {n} Sekunden.",
        "forwarded": "Weitergeleitete Nachrichten werden nicht verarbeitet; kopiere den Text, wenn du ihn meinst.",
        "new": "Neues Gespräch begonnen.",
        "terminal_only": "Nur im Terminal.",
        "not_here": "Über Telegram nicht verfügbar.",
        "locked": "Telegram ist gesperrt: dieser Chat steuert personal-agent nicht mehr. Im Terminal neu koppeln.",
        "inbound_off": "Chat über Telegram ist aus (PA_TELEGRAM_INBOUND).",
        "busy": "Beschäftigt: ein Durchgang läuft und {n} Nachrichten warten. Später erneut senden.",
        "queued": "In der Warteschlange ({n} davor).",
        "unavailable": "Der Assistent ist nicht verfügbar: {reason}",
        "turn_limit": "Nicht gesendet: das stündliche Limit für Modell-Durchgänge über Telegram ist erreicht "
                      "(PA_TELEGRAM_MAX_TURNS_PER_HOUR). note, tasks und help funktionieren weiter.",
        "error": "[Fehler] der Durchgang ist fehlgeschlagen ({reason})",
        "approvals": "Freigaben: Knöpfe unter jeder Anfrage; Stufe 3 braucht zusätzlich deine Freigabe-PIN als nächste Nachricht.",
        "cb_allowed": "Erlaubt",
        "cb_denied": "Abgelehnt",
        "cb_stopped": "Abgelehnt; der Durchgang wird gestoppt",
        "cb_need_pin": "Sende deine Freigabe-PIN als nächste Nachricht (2 Min.)",
        "cb_pin_busy": "Erst die andere PIN-Anfrage abschließen",
        "cb_pin_disabled": "Stufe-3-Freigaben über Telegram sind vorerst gesperrt",
        "cb_invalid": "Abgelaufen oder schon entschieden",
        "too_large": "[Freigabe für {tool} ist zu groß für die Prüfung aus der Ferne: abgelehnt]",
    },
}


def _texts(language: str) -> dict[str, str]:
    return TEXTS.get(language, TEXTS["en"])


def _int(value: object) -> bool:
    return type(value) is int


# --- outbox -------------------------------------------------------------------------


class Outbox:
    """Fire-and-forget sends and edits on their own thread (bounded; overflow drops with one log line)."""

    def __init__(self, client: TelegramClient, size: int = 100) -> None:
        self.client = client
        self._queue: queue.Queue = queue.Queue(maxsize=size)
        self._thread = threading.Thread(target=self._loop, name="telegram-outbox", daemon=True)
        self._overflow_logged = False
        self._closed = False
        self._thread.start()

    def __repr__(self) -> str:
        return "Outbox(<hidden>)"

    def put(self, job: Callable[[], None]) -> None:
        if self._closed:
            return
        try:
            self._queue.put_nowait(job)
            self._overflow_logged = False
        except queue.Full:
            if not self._overflow_logged:
                self._overflow_logged = True
                log.warning("telegram outbox full: message dropped")

    def send(self, chat_id: int, text: str, then: Callable[[int], None] | None = None) -> None:
        def job() -> None:
            for chunk in split_message(strip_invisible(text, keep="\n\t"), SAFE_TEXT_LIMIT, 5, utf16_len,
                                       REPLY_CUT_NOTE, keep="\n\t"):
                message_id = self.client.send_message(chat_id, chunk, deadline=time.monotonic() + REPLY_DEADLINE_S)
                if then is not None:
                    then(message_id)

        self.put(job)

    def _loop(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            try:
                job()
            except TelegramError as exc:
                log.warning("telegram send failed: %s", exc.kind)
            except Exception as exc:  # noqa: BLE001 - keep sending later messages
                log.warning("telegram send failed: %s", type(exc).__name__)

    def close(self, timeout_s: float = 2.0) -> None:
        self._closed = True
        try:
            self._queue.put(None, timeout=timeout_s)
        except queue.Full:
            return
        self._thread.join(timeout_s)


# --- the approval channel -------------------------------------------------------------


class TelegramChannel:
    """Approval channel of serve's Telegram assistant: asks with buttons through the inbox."""

    def __init__(self, inbox: ApprovalInbox, *, scope_name: str = "main", stop: Callable[[], bool],
                 stop_for: Callable[["ApprovalRequest"], bool], poll_s: float = 1.0,
                 note: Callable[[str], None] | None = None, on_stop: Callable[[], None] | None = None) -> None:
        self.inbox = inbox
        self.scope_name = scope_name
        self._stop = stop
        self._stop_for = stop_for
        self.poll_s = poll_s
        self.note = note
        self.on_stop = on_stop  # "Deny and stop turn": aborts the running turn

    def __repr__(self) -> str:
        return "TelegramChannel()"

    def _say(self, line: str) -> None:
        if self.note is not None:
            try:
                self.note(line)
            except Exception:  # noqa: BLE001 - a note never changes the decision
                pass

    def ask(self, request: "ApprovalRequest", deadline: float) -> bool:
        if request.scope != self.scope_name:
            return False  # serve runs no helpers: anything else is refused
        summary = RemoteChannel.page_summary(request)
        if request.full is not None or len(summary) > 2000:
            self._say(_texts(self.inbox.language)["too_large"].format(tool=request.tool_name))
            return False
        remaining = int(deadline - time.monotonic())
        if remaining <= EXPIRY_MARGIN_S:
            return False
        entry = self.inbox.register(request.tool_name, int(request.level), summary, remaining, via="telegram",
                                    origin=request.scope_label, context=bool(request.context), stoppable=True)
        if entry is None:
            return False
        state: str | None = "pending"
        try:
            while state == "pending":
                if self._stop() or self._stop_for(request):
                    break
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                state = self.inbox.wait(entry.id, min(self.poll_s, left, MAX_WAIT_S))
        finally:
            if state not in ("allowed", "denied", "expired"):
                self.inbox.cancel(entry.id)
        if entry.stop_requested and self.on_stop is not None:
            try:
                self.on_stop()
            except Exception as exc:  # noqa: BLE001
                log.warning("telegram stop failed: %s", type(exc).__name__)
        return state == "allowed"


# --- inbound --------------------------------------------------------------------------


class _StrangerLog:
    """At most one line per 10 min about ignored updates; no ids, no content."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self.clock = clock
        self.count = 0
        self.last: float | None = None
        self.lines = 0

    def note(self) -> None:
        self.count += 1
        now = self.clock()
        if self.last is None or now - self.last >= STRANGER_LOG_EVERY_S:
            log.info("telegram: ignored %d update(s) from other chats", self.count)
            self.lines += 1
            self.count = 0
            self.last = now


class TelegramFrontend:
    """Owner messages and button presses of one pairing generation."""

    def __init__(self, config: "Config", client: TelegramClient, owner: Owner, outbox: Outbox,
                 runtime_factory: Callable[[], "AssistantRuntime"], inbox: ApprovalInbox | None,
                 notifier: "Notifier | None", offsets: OffsetStore, *, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic, on_lock: Callable[[], None] | None = None) -> None:
        self.config = config
        self.client = client
        self.owner = owner
        self.outbox = outbox
        self.runtime_factory = runtime_factory
        self.inbox = inbox
        self.notifier = notifier
        self.offsets = offsets
        self.clock = clock
        self.monotonic = monotonic
        self.on_lock = on_lock
        self.texts = _texts(config.language)
        self._queue: queue.Queue = queue.Queue(maxsize=MAX_QUEUED_TURNS)
        self._busy = threading.Event()
        self._closed = threading.Event()
        self._runtime: "AssistantRuntime | None" = None
        self._runtime_lock = threading.Lock()
        self._unavailable_until = 0.0
        self._recent: deque[float] = deque()
        self._slow_told: float | None = None
        self._turns: deque[float] = deque()
        self._callbacks: dict[str, float] = {}
        self._halted_told = False
        self._last_turn = monotonic()
        self._last_message_id = offsets.last_owner_message_id(owner.chat_id)  # only this chat's ids count
        self.strangers = _StrangerLog(monotonic)
        self._worker = threading.Thread(target=self._work, name="telegram-turns", daemon=True)

    def __repr__(self) -> str:
        return "TelegramFrontend(<hidden>)"

    def start(self) -> None:
        self._worker.start()

    def reply(self, text: str, then: Callable[[int], None] | None = None) -> None:
        self.outbox.send(self.owner.chat_id, text, then)

    # --- dispatch (poller thread) ---

    def handle_update(self, update: dict) -> None:
        if not isinstance(update, dict) or self._closed.is_set():
            return  # after /lock (or close) the rest of a batch is never handled
        if "callback_query" in update:
            self._callback(update["callback_query"])
        elif "message" in update:
            self._message(update["message"])

    def _is_owner_message(self, message: object) -> bool:
        if not isinstance(message, dict):
            return False
        chat, sender = message.get("chat"), message.get("from")
        if not (isinstance(chat, dict) and isinstance(sender, dict) and _int(message.get("message_id"))
                and _int(message.get("date")) and _int(chat.get("id")) and _int(sender.get("id"))):
            return False
        text = message.get("text")
        if text is not None and (not isinstance(text, str) or len(text) > 4096):
            return False
        if any(message.get(name) is not None for name in FOREIGN_FIELDS):
            return False
        return (chat.get("type") == "private" and chat["id"] == self.owner.chat_id
                and sender["id"] == self.owner.user_id and sender.get("is_bot") is not True)

    def _message(self, message: object) -> None:
        if not self._is_owner_message(message):
            self.strangers.note()
            return
        assert isinstance(message, dict)
        message_id = message["message_id"]
        if self._last_message_id is not None and message_id <= self._last_message_id:
            return  # a replay after a lost offset: never handled twice
        self._last_message_id = message_id
        self.offsets.save(last_owner_message_id=message_id, chat_id=self.owner.chat_id)
        raw = message.get("text")
        text = raw.strip() if isinstance(raw, str) else None
        t = self.texts
        inbox = self.inbox

        # 1. The PIN never reaches the model, the conversation, the vault or a log line.
        waiting = inbox.pin_entry() if inbox is not None else None
        if text is not None and waiting is not None and PIN_LIKE.fullmatch(text):
            self._delete(message_id)
            self._submit_pin(waiting, text)
            return
        if text is not None and self._is_known_pin(text):
            self._delete(message_id)
            self.reply(t["pin_like"])
            return
        # /lock only reduces access: it works in every state, also during a PIN wait.
        if text is not None and text.lower() in ("/lock", "/unpair"):
            self._lock_out()
            return
        if waiting is not None:
            self.reply(t["pin_waiting"])
            return
        # 2. Old, too many, forwarded or not text.
        if self.clock() - message["date"] > STALE_MESSAGE_S:
            self.reply(t["stale"])
            return
        if not self._rate_ok():
            return
        if any(message.get(name) is not None for name in FORWARD_FIELDS):
            self.reply(t["forwarded"])
            return
        voice = message.get("voice")
        if not text and isinstance(voice, dict) and self.config.stt_command is not None:
            file_id, duration = voice.get("file_id"), voice.get("duration")
            if not isinstance(file_id, str) or not _int(duration):
                self.reply(t["no_text"])
                return
            if duration > self.config.voice_max_s:
                self.reply(t["voice_long"].format(n=self.config.voice_max_s))
                return
            if not self.config.telegram_inbound:
                self.reply(t["inbound_off"])
                return
            self._enqueue(("voice", file_id))
            return
        if not text:
            self.reply(t["no_text"])
            return
        # 3. Telegram-only commands.
        lowered = text.lower()
        if lowered == "/start" or lowered.startswith("/start "):
            self._help()
            return
        if lowered in ("quota reset", "/quota reset"):
            self.reply(t["terminal_only"])  # a compromised account must not clear the paid-usage protection
            return
        if lowered in ("/exit", "/quit"):
            self.reply(t["not_here"])
            return
        if lowered == "/new":
            self._enqueue(("new", None))
            return
        if not self.config.telegram_inbound:
            self.reply(t["inbound_off"])
            return
        self._enqueue(("turn", text))

    def _lock_out(self) -> None:
        """/lock from the owner chat: cut this chat off now, not at the next supervisor tick.

        Order: the pairing file goes first (no new prompt can be sent), then pending prompts of
        this chat are denied, the frontend stops taking updates, its queue is dropped unlogged
        and a running turn is stopped. The supervisor finishes the teardown.
        """
        self.reply(self.texts["locked"])
        if self.on_lock is not None:
            try:
                self.on_lock()
            except Exception as exc:  # noqa: BLE001 - the local cut-off below still happens
                log.warning("telegram lock failed: %s", type(exc).__name__)
        if self.inbox is not None:
            self.inbox.deny_telegram(self.owner.chat_id)
        self._closed.set()
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self.abort_turn()

    def _is_known_pin(self, text: str) -> bool:
        probe = text.encode("utf-8")
        found = False
        for pin in (self.config.approval_pin, self.config.call_pin):
            if pin and hmac.compare_digest(probe, pin.encode("utf-8")):
                found = True
        return found

    def _delete(self, message_id: int) -> None:
        chat = self.owner.chat_id
        self.outbox.put(lambda: self.client.delete_message(chat, message_id))

    def _submit_pin(self, entry: PendingApproval, pin: str) -> None:
        assert self.inbox is not None
        outcome = self.inbox.submit_pin(entry.id, pin)
        t = self.texts
        if outcome == "ok":
            self.reply(t["pin_ok"])
        elif outcome == "bad_pin":
            self.reply(t["pin_bad"].format(n=self.inbox.pin_attempts_left(entry)))
        elif outcome == "locked":
            self.reply(t["pin_locked"])
        else:
            self.reply(t["pin_invalid"])

    def _rate_ok(self) -> bool:
        now = self.monotonic()
        while self._recent and now - self._recent[0] >= 60:
            self._recent.popleft()
        if len(self._recent) >= MAX_OWNER_MESSAGES_PER_MIN:
            if self._slow_told is None or now - self._slow_told >= 60:
                self._slow_told = now
                self.reply(self.texts["slow"])
            return False
        self._recent.append(now)
        return True

    def _help(self) -> None:
        if not self.config.telegram_inbound:
            self.reply("\n".join((self.texts["inbound_off"], self.texts["approvals"])))
            return
        try:
            runtime = self._ensure_runtime()
        except Exception:  # noqa: BLE001 - the reason is told by _ensure_runtime's caller path
            runtime = None
        text = runtime.router.help_text() if runtime is not None else ""
        self.reply("\n".join(line for line in (text, self.texts["approvals"]) if line))

    def _transcribe(self, file_id: str | None) -> str | None:
        """A voice message as text (worker thread), or None after telling the owner why."""
        from .stt import MAX_AUDIO_BYTES, SttError, transcribe
        from .telegram import TelegramError

        try:
            audio = self.client.download(file_id or "", MAX_AUDIO_BYTES)
            text = transcribe(audio, self.config)
        except (SttError, TelegramError) as exc:
            reason = exc.args[0] if exc.args and isinstance(exc.args[0], str) else type(exc).__name__
            self.reply(self.texts["voice_failed"].format(reason=reason[:20]))
            return None
        if PIN_LIKE.fullmatch(text) or self._is_known_pin(text):
            self.reply(self.texts["pin_like"])  # a spoken PIN is never used
            return None
        self.reply(self.texts["heard"].format(text=telegram_untrusted(text, 400)))
        return text

    def _enqueue(self, item: tuple[str, str | None]) -> None:
        ahead = self._queue.qsize() + (1 if self._busy.is_set() else 0)
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            self.reply(self.texts["busy"].format(n=MAX_QUEUED_TURNS))
            return
        if ahead:
            self.reply(self.texts["queued"].format(n=ahead))

    # --- buttons (poller thread) ---

    def _callback(self, query: object) -> None:
        if not isinstance(query, dict):
            self.strangers.note()
            return
        query_id, sender, message, data = query.get("id"), query.get("from"), query.get("message"), query.get("data")
        chat = message.get("chat") if isinstance(message, dict) else None
        if not (isinstance(query_id, str) and 0 < len(query_id) <= 64 and isinstance(sender, dict)
                and _int(sender.get("id")) and isinstance(chat, dict) and _int(chat.get("id"))
                and _int(message.get("message_id")) and isinstance(data, str)  # type: ignore[union-attr]
                and CALLBACK_DATA.fullmatch(data)):
            self.strangers.note()
            return
        if sender["id"] != self.owner.user_id or chat["id"] != self.owner.chat_id:
            self.strangers.note()
            return
        now = self.monotonic()
        self._callbacks = {k: v for k, v in self._callbacks.items() if now - v < CALLBACK_SEEN_S}
        if query_id in self._callbacks:
            return  # replayed callback
        self._callbacks[query_id] = now
        outcome = self.inbox.press(data, chat["id"], message["message_id"]) if self.inbox is not None \
            else "invalid"  # type: ignore[index]
        try:
            self.client.answer_callback_query(query_id, self.texts.get(f"cb_{outcome}", self.texts["cb_invalid"]))
        except TelegramError as exc:
            log.info("telegram answerCallbackQuery failed: %s", exc.kind)
        if outcome == "need_pin" and self.inbox is not None:
            entry = self.inbox.pin_entry()
            if entry is not None:
                def remember(message_id: int, entry: PendingApproval = entry) -> None:
                    entry.pin_message_id = message_id

                self.reply(self.texts["pin_prompt"].format(tool=clean_text(entry.tool_name, 80)), remember)

    # --- the worker ---

    def _ensure_runtime(self) -> "AssistantRuntime":
        with self._runtime_lock:
            if self._closed.is_set():
                raise RuntimeError("closing")
            if self._runtime is None:
                if self.monotonic() < self._unavailable_until:
                    raise RuntimeError("starting failed recently")
                try:
                    self._runtime = self.runtime_factory()
                except Exception:
                    self._unavailable_until = self.monotonic() + UNAVAILABLE_RETRY_S
                    raise
            return self._runtime

    def _turn_allowed(self, runtime: "AssistantRuntime", text: str) -> bool:
        """PA_TELEGRAM_MAX_TURNS_PER_HOUR for model turns; tier 1 stays free."""
        if runtime.router.is_tier1(text):
            return True
        now = self.monotonic()
        while self._turns and now - self._turns[0] >= 3600:
            self._turns.popleft()
        if len(self._turns) >= self.config.telegram_max_turns_per_hour:
            return False
        self._turns.append(now)
        return True

    def _work(self) -> None:
        from .assistant import AssistantError, reply_lines
        from .bridge import BridgeError

        while not self._closed.is_set():
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                self._maybe_idle_close()
                continue
            if item is None:
                return
            self._busy.set()
            try:
                kind, text = item
                try:
                    runtime = self._ensure_runtime()
                except AssistantError as exc:
                    self.reply(self.texts["unavailable"].format(reason=f"cannot start (code {exc.code})"))
                    continue
                except Exception as exc:  # noqa: BLE001
                    self.reply(self.texts["unavailable"].format(reason=type(exc).__name__))
                    continue
                if kind == "new":
                    runtime.reset_conversation()
                    self.reply(self.texts["new"])
                    continue
                if kind == "voice":
                    text = self._transcribe(text)
                    if text is None:
                        continue
                assert text is not None
                if not self._turn_allowed(runtime, text):
                    self.reply(self.texts["turn_limit"])
                    continue
                halted_before = runtime.router.halted
                try:
                    reply = runtime.turn(text)
                except BridgeError as exc:
                    if not self._closed.is_set():
                        self.reply(self.texts["error"].format(reason=type(exc).__name__))
                    continue
                finally:
                    self._last_turn = self.monotonic()
                if self._closed.is_set():
                    return
                lines = reply_lines(reply, runtime.parts.config_dir, prefix="", local_paths=False)
                self.reply("\n".join(lines))
                if runtime.router.halted and not halted_before and not self._halted_told:
                    self._halted_told = True
                    if self.notifier is not None:
                        self.notifier.send("info", info_notice("safety_stop", self.config.language))
            except Exception as exc:  # noqa: BLE001 - the worker keeps serving
                log.warning("telegram turn failed: %s", type(exc).__name__)
            finally:
                self._busy.clear()

    def _maybe_idle_close(self) -> None:
        runtime = self._runtime
        if runtime is not None and self.monotonic() - self._last_turn >= IDLE_CLOSE_S:
            self._last_turn = self.monotonic()
            try:
                runtime.idle_close()
            except Exception as exc:  # noqa: BLE001
                log.warning("telegram idle close failed: %s", type(exc).__name__)

    def abort_turn(self) -> None:
        """Stop the main Claude Code process of the running turn.

        An interrupt, not abort(): the session stays usable and the next turn resumes the
        conversation (abort() would refuse every later start of this long-lived session).
        """
        runtime = self._runtime
        if runtime is not None:
            runtime.main_session.interrupt()

    def close(self, join_s: float = 5.0) -> None:
        """Stop the worker without logging the queued messages; close the runtime (and its broker)."""
        self._closed.set()
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        with self._runtime_lock:
            runtime, self._runtime = self._runtime, None
        if runtime is not None:
            try:
                runtime.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("telegram assistant close failed: %s", type(exc).__name__)
        if self._worker.is_alive():
            self._worker.join(join_s)


# --- the poller -------------------------------------------------------------------------


class TelegramPoller:
    """getUpdates long polling. The offset is saved before a batch is handled (at most once)."""

    def __init__(self, client: TelegramClient, offsets: OffsetStore, handle: Callable[[dict], None], *,
                 stop: threading.Event, poll_timeout_s: int = POLL_TIMEOUT_S,
                 rng: Callable[[], float] = random.random,
                 on_conflict: Callable[[int], None] | None = None,
                 on_rejected: Callable[[], None] | None = None) -> None:
        self.client = client
        self.offsets = offsets
        self.handle = handle
        self.stop = stop
        self.poll_timeout_s = poll_timeout_s
        self.rng = rng
        self.on_conflict = on_conflict
        self.on_rejected = on_rejected
        self.limit = UPDATES_LIMIT
        self._oversize = 0
        self._failures = 0
        self._conflicts = 0

    def __repr__(self) -> str:
        return "TelegramPoller(<hidden>)"

    def poll_once(self) -> int:
        offset = self.offsets.load()
        try:
            updates = self.client.get_updates(offset, self.poll_timeout_s, self.limit)
        except TelegramError as exc:
            if exc.kind != "too_large":
                raise
            self._oversize += 1
            if self.limit > 1 and self._oversize >= 2:
                self.limit = 1
                log.warning("telegram: oversized updates, polling one at a time")
            elif self.limit == 1 and exc.first_update_id is not None:
                if not self.offsets.save(exc.first_update_id + 1):
                    raise TelegramError("state") from None
                log.warning("telegram: skipped one update that is too large to read")
            return 0
        self._oversize = 0
        self.limit = UPDATES_LIMIT
        valid = [u for u in updates
                 if isinstance(u, dict) and _int(u.get("update_id")) and u["update_id"] >= 0]
        if not updates:
            return 0
        ids = [u["update_id"] for u in valid]
        if ids:
            # At most once: a crash may lose a message, but never repeats a note, a task or a press.
            if not self.offsets.save(max(ids) + 1):
                raise TelegramError("state")
        for update in valid:
            try:
                self.handle(update)
            except Exception as exc:  # noqa: BLE001 - one bad update never stops the poller
                log.warning("telegram update failed: %s", type(exc).__name__)
        return len(valid)

    def _backoff(self) -> float:
        self._failures += 1
        return min(BACKOFF_CAP_S, 2 ** min(self._failures, 6)) * (0.5 + self.rng() / 2)

    def run(self) -> None:
        conflict_logged = 0.0
        while not self.stop.is_set():
            try:
                self.poll_once()
                self._failures = 0
            except TelegramError as exc:
                if exc.kind == "unauthorized":
                    log.error("telegram: the bot token was rejected; fix PA_TELEGRAM_TOKEN and restart serve")
                    if self.on_rejected is not None:
                        self.on_rejected()
                    return
                if exc.kind == "conflict":
                    if self.stop.is_set():
                        return  # the abandoned poll of a stopped generation: not a foreign poller
                    self._conflicts += 1
                    now = time.monotonic()
                    if now - conflict_logged >= 600:
                        conflict_logged = now
                        log.warning("telegram: another process polls this bot")
                    if self.on_conflict is not None:
                        self.on_conflict(self._conflicts)
                    self.stop.wait(CONFLICT_WAIT_S)
                    continue
                if exc.kind == "rate_limited":
                    self.stop.wait(min(exc.retry_after or 5.0, 300))
                    continue
                log.warning("telegram poll failed: %s%s", exc.kind, f" {exc.code}" if exc.code else "")
                self.stop.wait(self._backoff())
            except Exception as exc:  # noqa: BLE001
                log.warning("telegram poll failed: %s", type(exc).__name__)
                self.stop.wait(self._backoff())


# --- the service in serve -----------------------------------------------------------------


class _Generation:
    def __init__(self, owner: Owner, stop: threading.Event, outbox: Outbox, frontend: TelegramFrontend,
                 poller: TelegramPoller, thread: threading.Thread) -> None:
        self.owner = owner
        self.stop = stop
        self.outbox = outbox
        self.frontend = frontend
        self.poller = poller
        self.thread = thread


class TelegramService:
    """Runs the Telegram side of serve: the supervisor and, while paired, one generation."""

    def __init__(self, config: "Config", notifier: "Notifier", *, client: TelegramClient | None = None,
                 runtime_factory: Callable[["TelegramService"], "AssistantRuntime"] | None = None,
                 clock: Callable[[], float] = time.time, tick_s: float = SUPERVISOR_TICK_S,
                 poll_timeout_s: int = POLL_TIMEOUT_S) -> None:
        self.config = config
        self.notifier = notifier
        self.client = client or TelegramClient.from_config(config)
        self.owners = OwnerStore(config.state_dir, self.client.bot_id)
        self.offsets_store = OffsetStore(config.state_dir, self.client.bot_id)
        self.lock = PollLock(config.state_dir)
        self.target = TelegramTarget(self.client, self.owners)
        self.approval_sender = TelegramApprovalSender(self.client, self.owners, config.language,
                                                      config.public_approval_url)
        self.runtime_factory = runtime_factory
        self.clock = clock
        self.tick_s = tick_s
        self.poll_timeout_s = poll_timeout_s
        self.inbox: ApprovalInbox | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._gen: _Generation | None = None
        self._gen_lock = threading.Lock()
        self._closer: Outbox | None = None
        self.hijacked = False
        self._rejected_told = False
        self._conflicts = 0

    def __repr__(self) -> str:
        return "TelegramService(<hidden>)"

    @property
    def polling(self) -> bool:
        return self._gen is not None

    # --- approvals ---

    def on_final(self, entry: PendingApproval) -> None:
        """Close a Telegram prompt (buttons off, outcome shown) without blocking the caller."""
        if entry.via != "telegram" or entry.message_id is None:
            return
        closer = self._closer
        if closer is not None:
            closer.put(lambda: self.approval_sender.close(entry))

    # --- lifecycle ---

    def start(self, inbox: ApprovalInbox | None) -> None:
        self.inbox = inbox
        self._closer = Outbox(self.client)
        self._thread = threading.Thread(target=self._supervise, name="telegram-supervisor", daemon=True)
        self._thread.start()

    def _supervise(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - keep supervising
                log.warning("telegram supervisor round failed: %s", type(exc).__name__)
            self._stop.wait(self.tick_s)

    def tick(self) -> None:
        if self.inbox is not None:
            self.inbox.sweep()
        owner = self.owners.current()
        gen = self._gen
        if self.client.rejected and not self._rejected_told:
            self._rejected_told = True
            log.error("telegram: the bot token was rejected; Telegram is off until serve restarts")
            self.notifier.send("info", info_notice("telegram_rejected", self.config.language), exclude=("telegram",))
        if gen is not None:
            if (owner is None or owner != gen.owner or self.client.rejected or self.hijacked
                    or not gen.thread.is_alive()):
                self._stop_generation(old_chat=gen.owner.chat_id)
            return
        if owner is None or self.client.rejected or self.hijacked or self._stop.is_set():
            return
        if not self.lock.acquire():
            return  # `telegram pair` (or another serve) polls right now
        if self._webhook_set():
            self.lock.release()
            self._hijack()
            return
        self._start_generation(owner)

    def _webhook_set(self) -> bool:
        """Anyone holding the token can set a webhook and receive every update: check first."""
        try:
            info = self.client.get_webhook_info()
        except TelegramError as exc:
            log.info("telegram getWebhookInfo failed: %s", exc.kind)
            return False
        url = info.get("url")
        return isinstance(url, str) and url != ""

    def _hijack(self) -> None:
        if self.hijacked:
            return
        self.hijacked = True
        self.approval_sender.blocked = True  # no prompt goes out while someone else reads the updates
        self.target.blocked = True
        log.error("telegram: another client receives this bot's updates; the token may be leaked. "
                  "Telegram is off until serve restarts")
        if self.inbox is not None:
            self.inbox.deny_telegram()
        # Never deleteWebhook: that would hide the compromise.
        self.notifier.send("info", info_notice("telegram_hijack", self.config.language), exclude=("telegram",))

    def _on_conflict(self, count: int) -> None:
        if self._webhook_set() or count >= CONFLICTS_FOR_HIJACK:
            self._hijack()
            gen = self._gen
            if gen is not None:
                gen.stop.set()

    def _on_rejected(self) -> None:
        gen = self._gen
        if gen is not None:
            gen.stop.set()

    def _on_lock(self) -> None:
        """/lock from the owner: remove the pairing (only reduces access) and stop the poller now;
        the frontend denies its prompts itself, the supervisor finishes the teardown."""
        self.owners.remove()
        gen = self._gen
        if gen is not None:
            gen.stop.set()
        log.warning("telegram: locked from the owner chat; pair again in a terminal")

    def _default_runtime(self) -> "AssistantRuntime":
        if self.runtime_factory is not None:
            return self.runtime_factory(self)
        return build_telegram_runtime(self.config, self.inbox, self)

    def _start_generation(self, owner: Owner) -> None:
        stop = threading.Event()
        outbox = Outbox(self.client)
        frontend = TelegramFrontend(self.config, self.client, owner, outbox, self._default_runtime, self.inbox,
                                    self.notifier, self.offsets_store, clock=self.clock, on_lock=self._on_lock)
        poller = TelegramPoller(self.client, self.offsets_store, frontend.handle_update, stop=stop,
                                poll_timeout_s=self.poll_timeout_s, on_conflict=self._on_conflict,
                                on_rejected=self._on_rejected)
        thread = threading.Thread(target=poller.run, name="telegram-poller", daemon=True)
        gen = _Generation(owner, stop, outbox, frontend, poller, thread)
        frontend.start()
        thread.start()
        # Published only once its threads run, so close() never joins a thread that has not started.
        with self._gen_lock:
            closing = self._stop.is_set()
            if not closing:
                self._gen = gen
        if closing:
            self._end_generation(gen, None)
            return
        log.info("telegram: polling for the paired chat")

    def _stop_generation(self, old_chat: int | None = None) -> None:
        with self._gen_lock:
            gen, self._gen = self._gen, None
        if gen is not None:
            self._end_generation(gen, old_chat)

    def _end_generation(self, gen: _Generation, old_chat: int | None) -> None:
        gen.stop.set()
        if self.inbox is not None:
            self.inbox.deny_telegram(old_chat)
        gen.frontend.close()
        gen.outbox.close(2.0)
        gen.thread.join(1.0)  # an in-flight long poll is abandoned: the offset was saved before dispatch
        self.lock.release()
        log.info("telegram: stopped polling")

    def frontend(self) -> TelegramFrontend | None:
        gen = self._gen
        return gen.frontend if gen is not None else None

    def close(self, join_s: float = 8.0) -> None:
        self._stop.set()
        self._stop_generation()
        if self.inbox is not None:
            self.inbox.cancel_all()
        closer, self._closer = self._closer, None
        if closer is not None:
            closer.close(2.0)
        if self._thread is not None:
            self._thread.join(join_s)
        self.lock.release()


SERVE_HINT = "it runs when a chat is open; its approvals appear there"


def build_telegram_runtime(config: "Config", inbox: ApprovalInbox | None,
                           service: TelegramService | None = None) -> "AssistantRuntime":
    """The Telegram assistant: the same pieces as the chat, label "telegram", Telegram approvals,
    a board-only task service, no delegation, no session lists."""
    from .assistant import AssistantError, finish_assistant, prepare_assistant

    if inbox is None:
        raise AssistantError("remote approvals are not available", 1)
    holder: dict = {}

    def note(line: str) -> None:
        if service is not None:
            frontend = service.frontend()
            if frontend is not None:
                frontend.reply(line)

    def make_channel(broker: "ApprovalBroker") -> TelegramChannel:
        def on_stop() -> None:
            # "Deny and stop turn": interrupt only this turn; the session stays usable (never abort()).
            runtime = holder.get("runtime")
            if runtime is not None:
                runtime.main_session.interrupt()

        return TelegramChannel(inbox, stop=lambda: broker.closing.is_set(), stop_for=broker.request_stopped,
                               note=note, on_stop=on_stop)

    parts = prepare_assistant(config, label="telegram", channel=make_channel,
                              note=lambda line: log.info("telegram assistant: %s", clean_text(line, 120)),
                              warn=lambda line: log.warning("telegram assistant: %s", clean_text(line, 120)))
    tasks = None
    if config.tasks_enabled:
        from .coordinator import TaskService
        from .tasks import TaskBoard

        # Board only (decision D1: serve runs no helpers). Tasks queue here and run in a chat.
        tasks = TaskService(TaskBoard(config.state_dir), None, parts.latch, parts.vault, parts.domains,
                            standby=None, runner_hint=SERVE_HINT, origin="telegram",
                            allow_add=config.telegram_tasks == "queue")
    try:
        runtime = finish_assistant(parts, tasks=tasks, delegation=False, sessions=None, model=None,
                                   local_listings=False, note_source="telegram", channel="telegram")
    except AssistantError:
        parts.broker.close()
        raise
    holder["runtime"] = runtime
    return runtime


__all__ = [
    "MAX_QUEUED_TURNS", "SERVE_HINT", "Outbox", "TelegramChannel", "TelegramFrontend", "TelegramPoller",
    "TelegramService", "build_telegram_runtime",
]
