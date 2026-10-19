"""Telegram: the Bot API client, token redaction, the state files and the pairing.

The bot talks to the official Bot API over HTTPS with long polling (getUpdates).
There is no webhook mode and no inbound port. Only the standard library is used.

The bot token is a credential and part of every request URL
(`https://api.telegram.org/bot<token>/<method>`). It exists only in
`Config.telegram_token` and `TelegramClient._token`; the URL is a local variable
inside `TelegramClient.call` and never stored, logged or chained into an
exception: every urllib error becomes `TelegramError(kind, code)` raised after the
except block, so neither `__cause__` nor `__context__` carries the original error
(whose attributes hold the URL). `RedactingFilter` scrubs anything that still looks
like a bot token from every log record, tracebacks included.

Pairing binds exactly one owner: `personal-agent telegram pair` prints a one-time
deep-link code (192 bits, single use, 10 minutes, stored only as its SHA-256), and
the first private-chat `/start <code>` from a person (confirmed in the terminal)
binds that chat id and user id. Everything from any other chat or user is ignored
without a reply. The state files live in the state directory, are written
atomically with mode 0600 and are read with O_NOFOLLOW and a size cap.

The serve side (poller, inbound chat, approval buttons) is in telegram_bot.py.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import stat
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from . import __version__
from .notify import TELEGRAM_LIMIT, TELEGRAM_MAX_CHUNKS, defang, render_approval_notice, utf16_len
from .remote import _close_quietly, _NoRedirect
from .statefiles import read_private_json, remove_private, stat_key, write_private_json
from .status import clean_text
from .textsafe import strip_invisible

if TYPE_CHECKING:
    from .config import Config
    from .remote import PendingApproval

log = logging.getLogger(__name__)

OFFICIAL_API_BASE = "https://api.telegram.org"
# Not configurable from the environment: a stray loopback base in a real .env would send the
# token in clear text to whatever local process holds that port. Tests replace this value.
DEFAULT_API_BASE = OFFICIAL_API_BASE
TOKEN_RE = re.compile(r"^(\d{5,16}):([A-Za-z0-9_-]{30,64})$")
TOKEN_ANYWHERE_RE = re.compile(r"\d{5,16}:[A-Za-z0-9_-]{30,64}")
REDACTED = "<telegram-token>"
POLL_TIMEOUT_S = 25            # getUpdates timeout; the HTTP timeout is 10 s longer
HTTP_TIMEOUT_S = 15            # every other method
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_UPDATES_BYTES = 2 * 1024 * 1024
UPDATES_LIMIT = 20             # updates per getUpdates call (Telegram's default of 100 can exceed the cap)
API_TEXT_LIMIT = 4096          # UTF-16 code units per message
SAFE_TEXT_LIMIT = TELEGRAM_LIMIT
PAIR_CODE_BYTES = 24           # token_urlsafe: 32 characters of [A-Za-z0-9_-] (deep-link limit 64)
PAIR_TTL_S = 600
PAIR_MAX_FAILURES = 5          # wrong codes per sender: that sender is ignored for the rest of the window
PAIR_MAX_TOTAL_FAILURES = 50   # wrong codes from everyone: the pending code is dropped
SEND_GAP_S = 1.0               # per chat, between sendMessage/edit* calls
BACKOFF_BASE_S = 1
BACKOFF_CAP_S = 60
RETRY_AFTER_CAP_S = 300
STATE_CAP = 8192
OWNER_FILE = "telegram-owner.json"
PAIRING_FILE = "telegram-pairing.json"
OFFSET_FILE = "telegram-offset.json"
LOCK_FILE = "telegram.lock"
ALLOWED_UPDATES = ("message", "callback_query")
USERNAME_RE = re.compile(r"[A-Za-z0-9_]{5,32}")
CODE_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_FIRST_UPDATE = re.compile(rb'^\{"ok":\s*true,\s*"result":\s*\[\s*\{\s*"update_id":\s*(\d{1,15})\s*,')
_LOCK_FLAGS = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_SEND_METHODS = ("sendMessage", "editMessageText", "editMessageReplyMarkup", "deleteMessage")
_PACED_METHODS = ("sendMessage", "editMessageText", "editMessageReplyMarkup")


# --- redaction -----------------------------------------------------------------


_KNOWN_TOKENS: set[str] = set()
_KNOWN_LOCK = threading.Lock()


def redact(text: object, token: str | None = None) -> str:
    """text with the bot token (the given one, every configured one and anything shaped like a
    token, with or without the "bot" prefix of the URL path) replaced by <telegram-token>."""
    out = str(text)
    with _KNOWN_LOCK:
        tokens = set(_KNOWN_TOKENS)
    if token:
        tokens.add(token)
    for value in sorted(tokens, key=len, reverse=True):
        out = out.replace(value, REDACTED)
    return TOKEN_ANYWHERE_RE.sub(REDACTED, out)


def remember_token(token: str | None) -> None:
    """Redact this exact token everywhere from now on (in addition to the shape match)."""
    if token:
        with _KNOWN_LOCK:
            _KNOWN_TOKENS.add(token)


class RedactingFilter(logging.Filter):
    """Scrubs bot tokens from log records: the message, the arguments, tracebacks and stacks.

    A Formatter renders exc_info only after the filters ran, so the traceback is rendered
    here into exc_text, redacted, and exc_info cleared.
    """

    _formatter = logging.Formatter()

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken format string still gets logged, redacted
            message = f"{record.msg!s} {record.args!s}"
        record.msg = redact(message)
        record.args = ()
        if record.exc_info:
            try:
                text = self._formatter.formatException(record.exc_info)
            except Exception:  # noqa: BLE001
                text = "(traceback unavailable)"
            record.exc_text = redact(text)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


def install_redaction(config: "Config | None" = None) -> None:
    """Add a RedactingFilter to every root handler (idempotent) and remember the configured token."""
    if config is not None:
        remember_token(config.telegram_token)
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())


def install_excepthooks() -> None:
    """Uncaught exceptions (main and threads) are logged by type only, through the filtered logger."""

    def hook(exc_type, exc, tb) -> None:  # noqa: ANN001
        log.error("uncaught %s", getattr(exc_type, "__name__", "exception"))

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread is not None else "?"
        log.error("uncaught %s in thread %s", getattr(args.exc_type, "__name__", "exception"), name)

    sys.excepthook = hook
    threading.excepthook = thread_hook


# --- errors and the API base -----------------------------------------------------


class TelegramError(Exception):
    """str(e) is "telegram <kind> [<code>]": never a URL, a token or message text.

    kind: network | http | api | rate_limited | unauthorized | conflict | blocked |
    bad_response | too_large.
    """

    def __init__(self, kind: str, code: int | None = None, retry_after: float | None = None,
                 description: str = "", first_update_id: int | None = None) -> None:
        super().__init__(f"telegram {kind}" + (f" {code}" if code else ""))
        self.kind = kind
        self.code = code
        self.retry_after = retry_after
        self.description = description  # redacted, at most 80 characters, for one log line
        self.first_update_id = first_update_id  # too_large getUpdates: the first id in the prefix


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_api_base(base: str) -> str:
    """Exactly the official https base, or a loopback http(s) base (tests only). ValueError otherwise."""
    if base == OFFICIAL_API_BASE:
        return base
    try:
        parts = urllib.parse.urlsplit(base)
        host = parts.hostname
        port = parts.port  # raises for a bad port
    except ValueError:
        raise ValueError("the Telegram API base is not a valid URL") from None
    del port
    if (parts.scheme not in ("http", "https") or not host or not _loopback(host) or parts.path not in ("", "/")
            or parts.query or parts.fragment or parts.username is not None or parts.password is not None):
        raise ValueError("the Telegram API base must be https://api.telegram.org (or a loopback test server)")
    return base.rstrip("/")


# --- the client ------------------------------------------------------------------


def _int(value: object) -> bool:
    return type(value) is int


class TelegramClient:
    """A minimal Bot API client. Thread-safe; the token never leaves this object."""

    def __init__(self, token: str, api_base: str | None = None, *, timeout_s: float = HTTP_TIMEOUT_S,
                 opener: Callable | None = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
            raise ValueError("invalid Telegram bot token")
        self._token = token
        remember_token(token)
        self.api_base = validate_api_base(api_base if api_base is not None else DEFAULT_API_BASE)
        self.bot_id = int(token.split(":", 1)[0])
        self.timeout_s = timeout_s
        if opener is None:
            handlers: list = [_NoRedirect()]
            if self.api_base != OFFICIAL_API_BASE:
                handlers.insert(0, urllib.request.ProxyHandler({}))  # a loopback test server: never a proxy
            opener = urllib.request.build_opener(*handlers).open
        self._opener = opener
        self._sleep = sleep
        self._clock = clock
        self.rejected = False
        self._state_lock = threading.Lock()
        self._cooldown: dict[tuple[str, int | None], float] = {}
        self._next_slot: dict[int, float] = {}

    def __repr__(self) -> str:
        return "TelegramClient(<hidden>)"

    @classmethod
    def from_config(cls, config: "Config") -> "TelegramClient":
        if not config.telegram_token:
            raise ValueError("PA_TELEGRAM_TOKEN is not set")
        return cls(config.telegram_token)

    # --- low level ---

    @staticmethod
    def _method_class(method: str) -> str:
        if method in _SEND_METHODS:
            return "send"
        if method == "answerCallbackQuery":
            return "answer"
        if method == "getUpdates":
            return "poll"
        return "other"

    def _cooldown_key(self, method: str, params: dict) -> tuple[str, int | None]:
        chat = params.get("chat_id")
        return self._method_class(method), chat if _int(chat) else None

    def _before(self, method: str, params: dict, deadline: float | None) -> None:
        """Honour a 429 cooldown of this method class and chat, and pace sends per chat."""
        key = self._cooldown_key(method, params)
        with self._state_lock:
            until = self._cooldown.get(key, 0.0)
        now = self._clock()
        if until > now:
            if deadline is not None and until < deadline:
                self._sleep(until - now)
            else:
                raise TelegramError("rate_limited", 429, retry_after=until - now)
        chat = params.get("chat_id")
        if method in _PACED_METHODS and _int(chat):
            with self._state_lock:
                now = self._clock()
                slot = max(now, self._next_slot.get(chat, 0.0))
                self._next_slot[chat] = slot + SEND_GAP_S
            if slot > now:
                self._sleep(slot - now)

    def call(self, method: str, params: dict | None = None, *, timeout_s: float | None = None,
             deadline: float | None = None) -> object:
        """One request. Returns `result`; raises TelegramError (never with a URL or the token)."""
        if self.rejected:
            raise TelegramError("unauthorized", 401)
        params = dict(params or {})
        self._before(method, params, deadline)
        cap = MAX_UPDATES_BYTES if method == "getUpdates" else MAX_RESPONSE_BYTES
        body = json.dumps(params).encode("utf-8")
        failed = False
        status = 0
        raw = b""
        try:
            request = urllib.request.Request(f"{self.api_base}/bot{self._token}/{method}", data=body, method="POST")
            request.add_header("Content-Type", "application/json")
            request.add_header("User-Agent", f"personal-agent/{__version__}")
            with self._opener(request, timeout=timeout_s or self.timeout_s) as response:
                status = int(getattr(response, "status", 0) or response.getcode())
                raw = response.read(cap + 1)
        except urllib.error.HTTPError as exc:  # Telegram sends JSON with errors
            status = int(exc.code or 0)
            try:
                raw = exc.read(cap + 1) or b""
            except Exception:  # noqa: BLE001 - no body
                raw = b""
            _close_quietly(exc)
        except Exception:  # noqa: BLE001 - URLError, timeouts, OSError: their text can hold the URL
            failed = True
        # Raised after the except blocks: no __context__ or __cause__ with the original error.
        if failed:
            raise TelegramError("network")
        if len(raw) > cap:
            match = _FIRST_UPDATE.match(raw[:200]) if method == "getUpdates" else None
            raise TelegramError("too_large", status or None,
                                first_update_id=int(match.group(1)) if match else None)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            data = None
        if not isinstance(data, dict):
            raise TelegramError("http" if status >= 500 else "bad_response", status or None)
        if data.get("ok") is True and 200 <= status < 300:
            return data.get("result")
        code = data.get("error_code") if _int(data.get("error_code")) else status
        description = redact(clean_text(data.get("description"), 80), self._token)
        params_out = data.get("parameters") if isinstance(data.get("parameters"), dict) else {}
        if code in (401, 404):
            if method != "getMe" and not self._confirm_rejected():
                raise TelegramError("api", code, description=description)
            self.rejected = True
            raise TelegramError("unauthorized", code, description=description)
        if code == 409:
            raise TelegramError("conflict", code, description=description)
        if code == 429:
            retry = params_out.get("retry_after")
            retry_after = float(retry) if isinstance(retry, (int, float)) and not isinstance(retry, bool) else 5.0
            retry_after = max(0.0, min(retry_after, RETRY_AFTER_CAP_S))
            with self._state_lock:
                key = self._cooldown_key(method, params)
                self._cooldown[key] = max(self._cooldown.get(key, 0.0), self._clock() + retry_after)
            raise TelegramError("rate_limited", code, retry_after=retry_after, description=description)
        if code == 403:
            raise TelegramError("blocked", code, description=description)
        if _int(code) and 400 <= code < 500:
            raise TelegramError("api", code, description=description)
        raise TelegramError("http", code if _int(code) else None, description=description)

    def _confirm_rejected(self) -> bool:
        """A 401/404 from another method: only a second opinion from getMe marks the token rejected,
        so a misbehaving proxy or a transient 404 cannot switch Telegram off until restart."""
        try:
            self.call("getMe", {})
        except TelegramError as exc:
            return exc.kind == "unauthorized"
        return False

    def call_retrying(self, method: str, params: dict, *, deadline: float, max_attempts: int = 3) -> object:
        """Retries network, 5xx and bad responses (1, 2, 4 s) and a 429 whose wait fits the deadline."""
        attempt = 0
        while True:
            attempt += 1
            try:
                return self.call(method, params, deadline=deadline,
                                 timeout_s=max(1.0, min(self.timeout_s, deadline - self._clock())))
            except TelegramError as exc:
                if attempt >= max_attempts:
                    raise
                now = self._clock()
                if exc.kind in ("network", "http", "bad_response"):
                    wait = min(BACKOFF_BASE_S * 2 ** (attempt - 1), BACKOFF_CAP_S)
                elif exc.kind == "rate_limited" and exc.retry_after is not None:
                    wait = exc.retry_after
                else:
                    raise
                if now + wait >= deadline:
                    raise
                self._sleep(wait)

    # --- methods ---

    def get_me(self) -> dict:
        result = self.call("getMe", {})
        if not isinstance(result, dict) or not _int(result.get("id")):
            raise TelegramError("bad_response")
        return result

    def get_webhook_info(self) -> dict:
        result = self.call("getWebhookInfo", {})
        if not isinstance(result, dict):
            raise TelegramError("bad_response")
        return result

    def get_updates(self, offset: int | None, timeout_s: int = POLL_TIMEOUT_S,
                    limit: int = UPDATES_LIMIT) -> list:
        params: dict = {"timeout": int(timeout_s), "limit": int(limit), "allowed_updates": list(ALLOWED_UPDATES)}
        if offset is not None:
            params["offset"] = int(offset)
        result = self.call("getUpdates", params, timeout_s=timeout_s + 10)
        if not isinstance(result, list):
            raise TelegramError("bad_response")
        return result

    def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None, *, deadline: float,
                     max_attempts: int = 3) -> int:
        """Plain text (never a parse mode), link previews off. Returns the message id."""
        if utf16_len(text) > API_TEXT_LIMIT:
            raise ValueError("message too long for Telegram; split it first")
        params: dict = {"chat_id": int(chat_id), "text": text, "link_preview_options": {"is_disabled": True}}
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        result = self.call_retrying("sendMessage", params, deadline=deadline, max_attempts=max_attempts)
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if not _int(message_id):
            raise TelegramError("bad_response")
        return int(message_id)

    def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        params: dict = {"callback_query_id": callback_query_id}
        if text:
            params["text"] = text[:200]
        self.call("answerCallbackQuery", params)

    def edit_message_reply_markup(self, chat_id: int, message_id: int, reply_markup: dict | None = None) -> None:
        params: dict = {"chat_id": int(chat_id), "message_id": int(message_id),
                        "reply_markup": reply_markup or {"inline_keyboard": []}}
        self.call_retrying("editMessageReplyMarkup", params, deadline=self._clock() + 20)

    def edit_message_text(self, chat_id: int, message_id: int, text: str) -> None:
        if utf16_len(text) > API_TEXT_LIMIT:
            text = text[: SAFE_TEXT_LIMIT // 2]
        params = {"chat_id": int(chat_id), "message_id": int(message_id), "text": text,
                  "link_preview_options": {"is_disabled": True}}
        self.call_retrying("editMessageText", params, deadline=self._clock() + 20)

    def delete_message(self, chat_id: int, message_id: int) -> bool:
        """Best effort (used for PIN messages)."""
        try:
            self.call("deleteMessage", {"chat_id": int(chat_id), "message_id": int(message_id)})
        except TelegramError as exc:
            log.info("telegram deleteMessage failed: %s", exc.kind)
            return False
        return True


# --- state files ------------------------------------------------------------------


@dataclass(frozen=True)
class Owner:
    bot_id: int
    chat_id: int
    user_id: int
    paired_at: float

    def masked_chat(self) -> str:
        return "…" + str(self.chat_id)[-3:]


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class OwnerStore:
    """state_dir/telegram-owner.json: the one paired chat. Cached by (mtime, size, inode)."""

    def __init__(self, state_dir: Path, bot_id: int) -> None:
        self.state_dir = Path(state_dir)
        self.bot_id = bot_id
        self._lock = threading.Lock()
        self._key: tuple[int, int, int] | None = None
        self._owner: Owner | None = None
        self._warned = False

    def __repr__(self) -> str:
        return "OwnerStore(<hidden>)"

    def current(self) -> Owner | None:
        try:
            info = os.stat(self.state_dir / OWNER_FILE, follow_symlinks=False)
            key: tuple[int, int, int] | None = (info.st_mtime_ns, info.st_size, info.st_ino)
        except OSError:
            key = None
        with self._lock:
            if key is not None and key == self._key:
                return self._owner
        owner = self._read() if key is not None else None
        with self._lock:
            self._key, self._owner = key, owner
        return owner

    def _read(self) -> Owner | None:
        data = read_private_json(self.state_dir, OWNER_FILE, 1024, 1)
        if data is None:
            log.warning("telegram owner file ignored: invalid")
            return None
        bot_id, chat_id, user_id, paired_at = (data.get(k) for k in ("bot_id", "chat_id", "user_id", "paired_at"))
        if not (_int(bot_id) and _int(chat_id) and _int(user_id) and _number(paired_at)):
            log.warning("telegram owner file ignored: invalid")
            return None
        if chat_id != user_id or chat_id <= 0:
            log.warning("telegram owner file ignored: not a private chat")
            return None
        if bot_id != self.bot_id:
            if not self._warned:
                self._warned = True
                log.warning("telegram: the pairing belongs to another bot; pair this bot again")
            return None
        return Owner(bot_id, chat_id, user_id, float(paired_at))

    def save(self, owner: Owner) -> bool:
        return write_private_json(self.state_dir, OWNER_FILE, {
            "version": 1, "bot_id": owner.bot_id, "chat_id": owner.chat_id, "user_id": owner.user_id,
            "paired_at": owner.paired_at})

    def remove(self) -> bool:
        present = os.path.lexists(self.state_dir / OWNER_FILE)
        remove_private(self.state_dir, OWNER_FILE)
        return present


class PairingStore:
    """state_dir/telegram-pairing.json: the pending one-time code, as its SHA-256 only."""

    def __init__(self, state_dir: Path, bot_id: int) -> None:
        self.state_dir = Path(state_dir)
        self.bot_id = bot_id

    def __repr__(self) -> str:
        return "PairingStore(<hidden>)"

    def create(self, now: float) -> str:
        """A new code; returned once, stored only as a hash. Raises OSError if it cannot be stored."""
        code = secrets.token_urlsafe(PAIR_CODE_BYTES)
        payload = {"version": 1, "bot_id": self.bot_id, "code_sha256": hashlib.sha256(code.encode()).hexdigest(),
                   "created_at": now, "expires_at": now + PAIR_TTL_S, "failures": {}, "total_failures": 0}
        if not write_private_json(self.state_dir, PAIRING_FILE, payload):
            raise OSError("could not store the pairing code")
        return code

    def load(self) -> dict | None:
        data = read_private_json(self.state_dir, PAIRING_FILE, STATE_CAP, 1)
        if data is None or data.get("bot_id") != self.bot_id:
            return None
        digest = data.get("code_sha256")
        if not (isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)
                and _number(data.get("created_at")) and _number(data.get("expires_at"))
                and isinstance(data.get("failures"), dict) and _int(data.get("total_failures"))):
            return None
        return data

    def check(self, code: str, user_id: int, now: float) -> str:
        """ok | wrong | ignored | expired | none. ok and expired delete the file (single use)."""
        data = self.load()
        if data is None:
            return "none"
        if now >= data["expires_at"]:
            self.remove()
            return "expired"
        failures = data["failures"]
        mine = failures.get(str(user_id), 0)
        if _int(mine) and mine >= PAIR_MAX_FAILURES:
            return "ignored"
        given = hashlib.sha256(code.encode("utf-8")).hexdigest()
        if hmac.compare_digest(given, data["code_sha256"]):
            self.remove()
            return "ok"
        failures[str(user_id)] = (mine if _int(mine) else 0) + 1
        if len(failures) > PAIR_MAX_TOTAL_FAILURES:
            failures.pop(next(iter(failures)))
        data["total_failures"] += 1
        if data["total_failures"] >= PAIR_MAX_TOTAL_FAILURES:
            self.remove()
        else:
            write_private_json(self.state_dir, PAIRING_FILE, data)
        return "wrong"

    def remove(self) -> None:
        remove_private(self.state_dir, PAIRING_FILE)


class OffsetStore:
    """state_dir/telegram-offset.json: the next getUpdates offset and the last owner message id.

    `telegram pair` (another process) writes this file while serve runs, so the cache is
    validated by (mtime, size, inode) on every access, like OwnerStore. The last owner
    message id is stored with the chat it belongs to: message ids count per chat, so the
    replay guard of one chat must never apply to another (a re-pair to a new account).
    """

    def __init__(self, state_dir: Path, bot_id: int) -> None:
        self.state_dir = Path(state_dir)
        self.bot_id = bot_id
        self._lock = threading.Lock()
        self._cache: dict | None = None
        self._key: tuple[int, int, int] | None = None

    def _data(self) -> dict:
        key = stat_key(self.state_dir, OFFSET_FILE)
        if self._cache is None or key != self._key:
            data = read_private_json(self.state_dir, OFFSET_FILE, 1024, 1) if key is not None else None
            valid = (data is not None and data.get("bot_id") == self.bot_id and _int(data.get("offset"))
                     and data["offset"] >= 0)
            last = data.get("last_owner_message_id") if valid else None  # type: ignore[union-attr]
            chat = data.get("owner_chat_id") if valid else None  # type: ignore[union-attr]
            self._cache = {
                "offset": data["offset"] if valid else None,  # type: ignore[index]
                "last_owner_message_id": last if _int(last) else None,
                "owner_chat_id": chat if _int(chat) else None,
            }
            self._key = key
        return self._cache

    def load(self) -> int | None:
        with self._lock:
            return self._data()["offset"]

    def last_owner_message_id(self, chat_id: int | None = None) -> int | None:
        """The last handled owner message id; with chat_id, only if it belongs to that chat."""
        with self._lock:
            data = self._data()
            if chat_id is not None and data["owner_chat_id"] != chat_id:
                return None
            return data["last_owner_message_id"]

    def save(self, offset: int | None = None, last_owner_message_id: int | None = None,
             chat_id: int | None = None) -> bool:
        with self._lock:
            data = dict(self._data())
            if offset is not None:
                data["offset"] = int(offset)
            if last_owner_message_id is not None:
                data["last_owner_message_id"] = int(last_owner_message_id)
                data["owner_chat_id"] = int(chat_id) if chat_id is not None else None
            payload = {"version": 1, "bot_id": self.bot_id, "offset": data["offset"] or 0}
            if data["last_owner_message_id"] is not None:
                payload["last_owner_message_id"] = data["last_owner_message_id"]
                if data["owner_chat_id"] is not None:
                    payload["owner_chat_id"] = data["owner_chat_id"]
            if not write_private_json(self.state_dir, OFFSET_FILE, payload):
                return False
            self._cache = data
            self._key = stat_key(self.state_dir, OFFSET_FILE)
            return True


class PollLock:
    """An exclusive flock on state_dir/telegram.lock: only one process polls the bot (serve or pair)."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self._fd: int | None = None

    def _open(self) -> int | None:
        from .plugins import prepare_state_dir

        if prepare_state_dir(self.state_dir):
            return None
        try:
            fd = os.open(self.state_dir / LOCK_FILE, _LOCK_FLAGS, 0o600)
        except OSError:
            return None
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None
        return fd

    def acquire(self) -> bool:
        if self._fd is not None:
            return True
        fd = self._open()
        if fd is None:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                return False
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)  # also releases the flock

    @property
    def held(self) -> bool:
        return self._fd is not None

    def held_elsewhere(self) -> bool:
        """Whether another process polls right now (for `telegram status`)."""
        if self._fd is not None:
            return False
        if not os.path.lexists(self.state_dir / LOCK_FILE):
            return False
        if not self.acquire():
            return True
        self.release()
        return False


# --- outbound pieces ----------------------------------------------------------------


def inert(text: object, limit: int = 160) -> str:
    """Untrusted text as one inert line for Telegram (plain text, invisible characters gone, URLs defanged)."""
    flat = re.sub(r"\s+", " ", strip_invisible(str(text) if text is not None else "", keep="\n\t")).strip()
    if len(flat) > limit:
        flat = flat[: max(1, limit - 1)].rstrip() + "…"
    return defang(flat)


class TelegramTarget:
    """The paired owner chat as a notification channel (notify.Target)."""

    name = "telegram"
    limit = TELEGRAM_LIMIT
    max_chunks = TELEGRAM_MAX_CHUNKS

    def __init__(self, client: TelegramClient, owners: OwnerStore, clock: Callable[[], float] = time.monotonic) -> None:
        self.client = client
        self.owners = owners
        self.clock = clock

    def __repr__(self) -> str:
        return "TelegramTarget(<hidden>)"

    @classmethod
    def from_config(cls, config: "Config") -> "TelegramTarget":
        client = TelegramClient.from_config(config)
        return cls(client, OwnerStore(config.state_dir, client.bot_id))

    def measure(self, text: str) -> int:
        return utf16_len(text)

    def inert(self, text: object, limit: int = 160) -> str:
        return inert(text, limit)

    blocked = False  # set when another client receives this bot's updates (a leaked token)

    def ready(self) -> str | None:
        if self.client.rejected:
            return "telegram token rejected"
        if self.blocked:
            return "telegram blocked: another client receives this bot's updates"
        if self.owners.current() is None:
            return "telegram not paired"
        return None

    def post(self, text: str) -> bool:
        owner = self.owners.current()
        if owner is None:
            return False
        try:
            self.client.send_message(owner.chat_id, text, deadline=self.clock() + 30)
        except (TelegramError, ValueError) as exc:
            log.warning("telegram notice failed: %s", getattr(exc, "kind", type(exc).__name__))
            return False
        return True


BUTTONS = {
    "en": {"approve": "Approve", "deny": "Deny", "stop": "Deny and stop turn",
           "approved": "approved", "denied": "denied", "expired": "expired"},
    "de": {"approve": "Erlauben", "deny": "Ablehnen", "stop": "Ablehnen und stoppen",
           "approved": "erlaubt", "denied": "abgelehnt", "expired": "abgelaufen"},
}


class TelegramApprovalSender:
    """Sends approval prompts with inline buttons and closes them after the decision.

    The callback data of each button is a separate random one-time token from the inbox:
    no id, tool name, level or decision word inside.
    """

    def __init__(self, client: TelegramClient, owners: OwnerStore, language: str = "en",
                 public_base: str | None = None, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.client = client
        self.owners = owners
        self.language = language if language in BUTTONS else "en"
        self.public_base = public_base.rstrip("/") if public_base else None
        self.clock = clock
        self.monotonic = monotonic

    def __repr__(self) -> str:
        return "TelegramApprovalSender(<hidden>)"

    def _texts(self) -> dict[str, str]:
        return BUTTONS[self.language]

    def render(self, entry: "PendingApproval", link: str | None) -> str:
        minutes = max(1, round((entry.expires_at - self.clock()) / 60))
        return render_approval_notice("telegram", entry.tool_name, entry.level, entry.origin, entry.context,
                                      minutes=minutes, link=link, language=self.language, sequence=entry.sequence)

    def keyboard(self, entry: "PendingApproval") -> dict:
        t = self._texts()
        rows = [[{"text": t["approve"], "callback_data": entry.approve_token},
                 {"text": t["deny"], "callback_data": entry.deny_token}]]
        if entry.stop_token:
            rows.append([{"text": t["stop"], "callback_data": entry.stop_token}])
        for row in rows:
            for button in row:
                assert isinstance(button["callback_data"], str) and len(button["callback_data"].encode()) <= 64
        return {"inline_keyboard": rows}

    blocked = False  # set when another client receives this bot's updates (a leaked token)

    def send(self, entry: "PendingApproval") -> tuple[int, int] | None:
        owner = self.owners.current()
        if owner is None or self.client.rejected or self.blocked:
            return None
        link = f"{self.public_base}/approve/{entry.id}" if self.public_base else None
        entry.prompt_text = self.render(entry, None)  # the close edit drops the review link
        remaining = entry.expires_at - self.clock()
        deadline = self.monotonic() + max(1.0, min(remaining, 20.0))
        try:
            message_id = self.client.send_message(owner.chat_id, self.render(entry, link), self.keyboard(entry),
                                                  deadline=deadline)
        except (TelegramError, ValueError) as exc:
            log.warning("telegram approval prompt failed: %s", getattr(exc, "kind", type(exc).__name__))
            return None
        return owner.chat_id, message_id

    def close(self, entry: "PendingApproval") -> None:
        """Remove the buttons and say how it ended. Best effort."""
        if entry.chat_id is None or entry.message_id is None:
            return
        t = self._texts()
        outcome = t[{"allowed": "approved", "denied": "denied"}.get(entry.state, "expired")]
        try:
            self.client.edit_message_reply_markup(entry.chat_id, entry.message_id, {"inline_keyboard": []})
        except TelegramError as exc:
            log.info("telegram prompt close failed: %s", exc.kind)
        try:
            base = entry.prompt_text or self.render(entry, None)
            self.client.edit_message_text(entry.chat_id, entry.message_id, f"{base}\n→ {outcome}")
        except TelegramError as exc:
            log.info("telegram prompt edit failed: %s", exc.kind)


# --- pairing ------------------------------------------------------------------------


def _wallclock() -> float:
    return time.time()


def _stdout_tty() -> bool:
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError, OSError):
        return False


def _stdin_confirm(question: str) -> bool | None:
    """y/N from the terminal; None without a terminal."""
    try:
        if not sys.stdin.isatty():
            return None
        return input(question).strip().lower() in ("y", "yes", "j", "ja")
    except (EOFError, OSError, ValueError):
        return False


PAIRED_TEXT = {
    "en": "Paired. This chat now controls personal-agent. Approvals of level 3 need your approval PIN.",
    "de": "Gekoppelt. Dieser Chat steuert jetzt personal-agent. Freigaben der Stufe 3 brauchen deine Freigabe-PIN.",
}
REPAIRED_TEXT = {
    "en": "This bot was paired to another chat. This chat no longer controls personal-agent.",
    "de": "Dieser Bot wurde mit einem anderen Chat gekoppelt. Dieser Chat steuert personal-agent nicht mehr.",
}


def _pair_message(update: object, created_at: float, username: str) -> tuple[int, int, str, str] | None:
    """(chat id, user id, code, sender username) for a valid /start <code> in a private chat, else None."""
    if not isinstance(update, dict):
        return None
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    chat, sender, text, date = message.get("chat"), message.get("from"), message.get("text"), message.get("date")
    if not (isinstance(chat, dict) and isinstance(sender, dict) and isinstance(text, str) and _int(date)):
        return None
    chat_id, user_id = chat.get("id"), sender.get("id")
    if (chat.get("type") != "private" or not _int(chat_id) or chat_id != user_id or sender.get("is_bot") is True
            or date < created_at - 5 or message.get("business_connection_id") is not None):
        return None
    match = re.fullmatch(rf"/start(?:@{re.escape(username)})? ([A-Za-z0-9_-]{{1,64}})", text.strip())
    if match is None:
        return None
    shown = sender.get("username")
    shown = f"@{shown}" if isinstance(shown, str) and USERNAME_RE.fullmatch(shown) else "no username"
    return chat_id, user_id, match.group(1), shown


def run_pairing(config: "Config", *, force: bool = False, yes: bool = False, print_code: bool = False,
                out: Callable[[str], None] = print, err: Callable[[str], None] | None = None,
                client: TelegramClient | None = None, confirm: Callable[[str], bool | None] | None = None,
                stdout_tty: Callable[[], bool] | None = None, clock: Callable[[], float] | None = None,
                poll_timeout_s: int = POLL_TIMEOUT_S, sleep: Callable[[float], None] = time.sleep) -> int:
    """`personal-agent telegram pair`. Exit codes: 0 paired, 1 failed or refused, 2 usage, 130 interrupted."""
    from .config import ConfigError, validate_pin

    err = err or (lambda line: print(line, file=sys.stderr))
    clock = clock or _wallclock
    confirm = confirm or _stdin_confirm
    if not config.telegram_token:
        err("[error] PA_TELEGRAM_TOKEN is not set")
        return 2
    try:
        validate_pin(config.approval_pin, "PA_APPROVAL_PIN")
    except ConfigError as exc:
        err(f"[warning] {exc}: serve will refuse to start until it is set")
    if not (stdout_tty or _stdout_tty)() and not print_code:
        err("[error] stdout is not a terminal: the pairing code is not printed (pass --print-code to show it)")
        return 2
    client = client or TelegramClient.from_config(config)
    lock = PollLock(config.state_dir)
    if not lock.acquire():
        err("[error] another process is polling this bot (serve or a second pair); stop it or run "
            "personal-agent telegram unpair first")
        return 1
    owners = OwnerStore(config.state_dir, client.bot_id)
    pairing = PairingStore(config.state_dir, client.bot_id)
    offsets = OffsetStore(config.state_dir, client.bot_id)
    try:
        try:
            me = client.get_me()
        except TelegramError as exc:
            err(f"[error] the bot could not be reached ({exc})")
            return 1
        username = me.get("username")
        if me.get("id") != client.bot_id or me.get("is_bot") is not True or not (
                isinstance(username, str) and USERNAME_RE.fullmatch(username)):
            err("[error] the token does not belong to a usable bot")
            return 1
        previous = owners.current()
        if previous is not None and not force:
            err("[error] already paired; run personal-agent telegram unpair first (or pair --force)")
            return 1
        try:
            code = pairing.create(clock())
        except OSError:
            err("[error] could not store the pairing code in the state directory")
            return 1
        data = pairing.load() or {}
        created_at, expires_at = float(data.get("created_at", clock())), float(data.get("expires_at", clock()))
        out(f'Open https://t.me/{username}?start={code} on your phone (or send "/start {code}" to @{username}). '
            "The code works once and expires in 10 minutes.")
        failures = 0
        offset = offsets.load()
        while clock() < expires_at:
            try:
                updates = client.get_updates(offset, timeout_s=max(1, min(poll_timeout_s, int(expires_at - clock()))))
                failures = 0
            except TelegramError as exc:
                if exc.kind == "unauthorized":
                    err("[error] the bot token was rejected")
                    return 1
                failures += 1
                sleep(min(BACKOFF_CAP_S, 2 ** min(failures, 6)) if exc.kind != "conflict" else 30)
                continue
            ids = [u.get("update_id") for u in updates if isinstance(u, dict) and _int(u.get("update_id"))]
            if ids:
                offset = max(ids) + 1
                if not offsets.save(offset):
                    err("[error] could not save the update offset")
                    return 1
            for update in updates:
                found = _pair_message(update, created_at, username)
                if found is None:
                    continue
                chat_id, user_id, given, shown = found
                result = pairing.check(given, user_id, clock())
                if result in ("wrong", "ignored"):
                    if pairing.load() is None:
                        err("[error] too many wrong codes: the pairing code was dropped")
                        return 1
                    continue
                if result != "ok":
                    err("[error] the pairing code expired")
                    return 1
                out(f"request from user id …{str(user_id)[-4:]} ({shown})")
                if not yes:
                    answer = confirm("bind this account? [y/N] ")
                    if answer is None:
                        err("[error] no terminal to confirm: pass --yes to bind without asking")
                        return 1
                    if not answer:
                        err("[not paired]")
                        return 1
                owner = Owner(client.bot_id, chat_id, user_id, clock())
                if not owners.save(owner):
                    err("[error] could not save the pairing")
                    return 1
                offsets.save(last_owner_message_id=0, chat_id=chat_id)
                language = config.language if config.language in PAIRED_TEXT else "en"
                deadline = time.monotonic() + 20
                try:
                    client.send_message(chat_id, PAIRED_TEXT[language], deadline=deadline)
                    if previous is not None and previous.chat_id != chat_id:
                        client.send_message(previous.chat_id, REPAIRED_TEXT[language], deadline=deadline)
                except TelegramError as exc:
                    log.info("telegram pairing notice failed: %s", exc.kind)
                out(f"paired (chat {owner.masked_chat()})")
                return 0
        pairing.remove()
        err("[error] the pairing code expired")
        return 1
    except KeyboardInterrupt:
        pairing.remove()
        return 130
    finally:
        lock.release()


__all__ = [
    "DEFAULT_API_BASE", "TOKEN_RE", "Owner", "OwnerStore", "OffsetStore", "PairingStore", "PollLock", "RedactingFilter",
    "TelegramApprovalSender", "TelegramClient", "TelegramError", "TelegramTarget", "inert", "install_excepthooks",
    "install_redaction", "redact", "remember_token", "run_pairing", "validate_api_base",
]
