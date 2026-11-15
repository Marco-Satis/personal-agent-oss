"""Talk to the assistant over ntfy: the owner writes into the same topic the agent posts to.

One topic for notices and conversation (Marco 15.11.2026: "messages and answers in one place").
`serve` polls the topic (PA_NTFY_INBOUND=on, default off) and treats every message that is not
the agent's own as the owner's message:

- Own messages are skipped twice over: every post of the agent carries the tag `pa-agent`
  (remote.NTFY_OWN_TAG) and its id is remembered (NTFY_OWN). A loop would need both to fail.
- Who may write is decided by the ntfy server: the topic must be deny-all except the agent's and
  the owner's user. Anyone who can write to it is treated as the owner.
- The turn runs through the same Router as the chat and Telegram: tier-1 commands, notes, questions.
  Approvals never happen over ntfy (no buttons): the approval channel of this assistant denies
  every level-2/3 request and says where to approve instead. The broker enforces that, not a prompt.
- Text that looks like a PIN (6+ digits) or equals a configured PIN is never handled.
- Messages older than STALE_S, more than MAX_PER_MIN per minute, and model turns beyond
  PA_TELEGRAM_MAX_TURNS_PER_HOUR are refused with a short reply.
- The last handled id is kept in state_dir/ntfy_inbox.json; on the first start the inbox begins
  at "now", so nothing older is replayed. Message text is never logged.
"""

from __future__ import annotations

import collections
import hmac
import logging
import queue
import re
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .remote import NTFY_OWN_TAG, NtfyClient, OwnMessages
from .statefiles import read_private_json, write_private_json

if TYPE_CHECKING:
    from .approval import ApprovalBroker, ApprovalRequest
    from .assistant import AssistantRuntime
    from .config import Config

log = logging.getLogger(__name__)

NTFY_OWN = OwnMessages()  # process-wide: the notifier and the inbox share it
STATE_FILE = "ntfy_inbox.json"
STATE_VERSION = 1
POLL_S = 5.0
MAX_BACKOFF_S = 300.0
STALE_S = 600
MAX_PER_MIN = 20
MAX_TEXT = 4000
MAX_QUEUED = 5
IDLE_CLOSE_S = 1800
PIN_LIKE = re.compile(r"[0-9]{6,32}")
_ID_RE = re.compile(r"[A-Za-z0-9]{1,64}")

TEXTS = {
    "de": {
        "approval": "Das braucht eine Freigabe ({tool}). Über ntfy gibt es keine Freigaben: bitte im Chat oder "
                    "auf der Freigabe-Seite erledigen.",
        "pin": "Das sieht wie eine PIN aus. PINs gehören nicht in diesen Kanal; die Nachricht wurde nicht verarbeitet.",
        "stale": "Diese Nachricht ist älter als 10 Minuten und wurde nicht verarbeitet.",
        "slow": "Zu viele Nachrichten in kurzer Zeit. Bitte kurz warten.",
        "busy": "Es warten schon {n} Nachrichten. Bitte warten.",
        "limit": "Stundenlimit für Modell-Antworten erreicht. Feste Befehle (help, status) gehen weiter.",
        "new": "Neues Gespräch begonnen.",
        "unavailable": "Der Assistent ist gerade nicht erreichbar ({reason}).",
        "error": "Die Antwort ist fehlgeschlagen ({reason}).",
    },
    "en": {
        "approval": "This needs an approval ({tool}). ntfy has no approvals: please use the chat or the approval page.",
        "pin": "That looks like a PIN. PINs do not belong in this channel; the message was not handled.",
        "stale": "This message is older than 10 minutes and was not handled.",
        "slow": "Too many messages at once. Please wait a moment.",
        "busy": "{n} messages are already waiting. Please wait.",
        "limit": "Hourly limit for model replies reached. Fixed commands (help, status) still work.",
        "new": "Started a new conversation.",
        "unavailable": "The assistant is not available right now ({reason}).",
        "error": "The reply failed ({reason}).",
    },
}


def texts(language: str) -> dict[str, str]:
    return TEXTS.get(language, TEXTS["en"])


class DenyingChannel:
    """Approval channel of the ntfy assistant: every level-2/3 request is denied, with a hint."""

    def __init__(self, say: Callable[[str], None], language: str = "en") -> None:
        self.say = say
        self.language = language

    def ask(self, request: "ApprovalRequest", deadline: float) -> bool:
        from .status import clean_text

        try:
            self.say(texts(self.language)["approval"].format(tool=clean_text(request.tool_name, 60)))
        except Exception as exc:  # noqa: BLE001 - the denial stands even if the hint fails
            log.warning("ntfy approval hint failed: %s", type(exc).__name__)
        return False


def is_own(message: dict, own: OwnMessages) -> bool:
    tags = message.get("tags")
    return (isinstance(tags, list) and NTFY_OWN_TAG in tags) or message.get("id") in own


class NtfyInbox:
    """Polls the topic, filters, and runs one owner turn at a time on a worker thread."""

    def __init__(self, config: "Config", client: NtfyClient, runtime_factory: Callable[[], "AssistantRuntime"], *,
                 own: OwnMessages = NTFY_OWN, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.config = config
        self.client = client
        self.runtime_factory = runtime_factory
        self.own = own
        self.clock = clock
        self.monotonic = monotonic
        self.texts = texts(config.language)
        self.state_dir = Path(config.state_dir)
        self._queue: queue.Queue = queue.Queue(maxsize=MAX_QUEUED)
        self._recent: collections.deque[float] = collections.deque()
        self._turns: collections.deque[float] = collections.deque()
        self._runtime: "AssistantRuntime | None" = None
        self._last_turn = monotonic()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._since = self._load_since()

    def __repr__(self) -> str:
        return "NtfyInbox(<hidden>)"

    # --- state ---

    def _load_since(self) -> str:
        data = read_private_json(self.state_dir, STATE_FILE, 4096, STATE_VERSION) or {}
        last = data.get("last_id")
        if isinstance(last, str) and _ID_RE.fullmatch(last):
            return last
        return str(int(self.clock()))  # first start: nothing older is replayed

    def _save_since(self, message_id: str) -> None:
        self._since = message_id
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            write_private_json(self.state_dir, STATE_FILE, {"version": STATE_VERSION, "last_id": message_id})
        except OSError as exc:
            log.warning("ntfy inbox state not saved: %s", type(exc).__name__)

    # --- sending ---

    def reply(self, text: str) -> None:
        from .notify import NTFY_LIMIT, split_message, utf8_len

        for chunk in split_message(text, NTFY_LIMIT, 3, utf8_len):
            if not self.client.post(chunk):
                break

    # --- polling (poller thread) ---

    def poll_once(self) -> int:
        """Fetch and dispatch new messages. Returns how many were taken as owner messages."""
        messages = self.client.poll(self._since)
        taken = 0
        for message in messages:
            message_id = message.get("id")
            if message.get("event") != "message" or not isinstance(message_id, str) or not _ID_RE.fullmatch(message_id):
                continue
            self._save_since(message_id)
            if is_own(message, self.own):
                continue  # never answer the agent's own notices or replies
            taken += 1
            self.handle(message)
        return taken

    def handle(self, message: dict) -> None:
        raw = message.get("message")
        text = raw.strip()[:MAX_TEXT] if isinstance(raw, str) else ""
        if not text:
            return
        t = self.texts
        if PIN_LIKE.fullmatch(text) or self._is_known_pin(text):
            self.reply(t["pin"])
            return
        sent_at = message.get("time")
        if not isinstance(sent_at, (int, float)) or self.clock() - sent_at > STALE_S:
            self.reply(t["stale"])
            return
        if not self._rate_ok():
            return
        if text.lower() in ("/new", "neu", "/neu"):
            self._enqueue(("new", None))
            return
        self._enqueue(("turn", text))

    def _is_known_pin(self, text: str) -> bool:
        probe = text.encode("utf-8")
        found = False
        for pin in (self.config.approval_pin, self.config.call_pin):
            if pin and hmac.compare_digest(probe, pin.encode("utf-8")):
                found = True
        return found

    def _rate_ok(self) -> bool:
        now = self.monotonic()
        while self._recent and now - self._recent[0] >= 60:
            self._recent.popleft()
        if len(self._recent) >= MAX_PER_MIN:
            if len(self._recent) == MAX_PER_MIN:
                self.reply(self.texts["slow"])
                self._recent.append(now)
            return False
        self._recent.append(now)
        return True

    def _enqueue(self, item: tuple[str, str | None]) -> None:
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            self.reply(self.texts["busy"].format(n=MAX_QUEUED))

    # --- the worker ---

    def _turn_allowed(self, runtime: "AssistantRuntime", text: str) -> bool:
        if runtime.router.is_tier1(text):
            return True
        now = self.monotonic()
        while self._turns and now - self._turns[0] >= 3600:
            self._turns.popleft()
        if len(self._turns) >= self.config.telegram_max_turns_per_hour:
            return False
        self._turns.append(now)
        return True

    def work_once(self, timeout: float = 1.0) -> bool:
        """Handle one queued item. False when the queue was empty."""
        from .assistant import reply_lines
        from .bridge import BridgeError

        try:
            item = self._queue.get(timeout=timeout)
        except queue.Empty:
            self._maybe_idle_close()
            return False
        if item is None:
            return False
        kind, text = item
        try:
            if self._runtime is None:
                self._runtime = self.runtime_factory()
        except Exception as exc:  # noqa: BLE001 - told to the owner, the inbox keeps running
            self.reply(self.texts["unavailable"].format(reason=type(exc).__name__))
            return True
        runtime = self._runtime
        if kind == "new":
            runtime.reset_conversation()
            self.reply(self.texts["new"])
            return True
        assert text is not None
        if not self._turn_allowed(runtime, text):
            self.reply(self.texts["limit"])
            return True
        try:
            result = runtime.turn(text)
        except BridgeError as exc:
            self.reply(self.texts["error"].format(reason=type(exc).__name__))
            return True
        finally:
            self._last_turn = self.monotonic()
        self.reply("\n".join(reply_lines(result, runtime.parts.config_dir, prefix="", local_paths=False)))
        return True

    def _maybe_idle_close(self) -> None:
        if self._runtime is not None and self.monotonic() - self._last_turn >= IDLE_CLOSE_S:
            self._last_turn = self.monotonic()
            try:
                self._runtime.idle_close()
            except Exception as exc:  # noqa: BLE001
                log.warning("ntfy idle close failed: %s", type(exc).__name__)

    # --- threads ---

    def start(self) -> None:
        for target, name in ((self._poll_loop, "ntfy-poll"), (self._work_loop, "ntfy-work")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        log.info("ntfy inbox on: messages in the notice topic are read as yours (approvals never)")

    def _poll_loop(self) -> None:
        delay = POLL_S
        while not self._stop.is_set():
            try:
                self.poll_once()
                delay = POLL_S
            except Exception as exc:  # noqa: BLE001 - network or server trouble: back off and retry
                log.warning("ntfy poll failed: %s", type(exc).__name__)
                delay = min(MAX_BACKOFF_S, delay * 2)
            self._stop.wait(delay)

    def _work_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.work_once()
            except Exception as exc:  # noqa: BLE001 - the worker keeps serving
                log.warning("ntfy turn failed: %s", type(exc).__name__)

    def close(self, join_s: float = 5.0) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        for thread in self._threads:
            thread.join(join_s)
        if self._runtime is not None:
            try:
                self._runtime.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("ntfy runtime close failed: %s", type(exc).__name__)


def build_ntfy_runtime(config: "Config", say: Callable[[str], None]) -> "AssistantRuntime":
    """The ntfy assistant: the chat's pieces, label "ntfy", an approval channel that always denies,
    no tasks, no delegation, no session lists, mail under the remote rules."""
    from .assistant import AssistantError, finish_assistant, prepare_assistant
    from .status import clean_text

    def make_channel(broker: "ApprovalBroker") -> DenyingChannel:
        return DenyingChannel(say, config.language)

    parts = prepare_assistant(config, label="ntfy", channel=make_channel,
                              note=lambda line: log.info("ntfy assistant: %s", clean_text(line, 120)),
                              warn=lambda line: log.warning("ntfy assistant: %s", clean_text(line, 120)))
    try:
        return finish_assistant(parts, tasks=None, delegation=False, sessions=None, model=None,
                                local_listings=False, note_source="ntfy", channel="ntfy")
    except AssistantError:
        parts.broker.close()
        raise


def start_ntfy_inbox(config: "Config") -> NtfyInbox | None:
    """serve: start the inbox when PA_NTFY_INBOUND=on (config checks URL and token)."""
    if not (config.ntfy_inbound and config.ntfy_url):
        return None
    client = NtfyClient(config.ntfy_url, config.ntfy_token, own=NTFY_OWN)
    holder: dict = {}

    def say(text: str) -> None:
        inbox = holder.get("inbox")
        if inbox is not None:
            inbox.reply(text)

    inbox = NtfyInbox(config, client, lambda: build_ntfy_runtime(config, say))
    holder["inbox"] = inbox
    inbox.start()
    return inbox


__all__ = ["NTFY_OWN", "DenyingChannel", "NtfyInbox", "build_ntfy_runtime", "is_own", "start_ntfy_inbox"]
