"""The mail worker: the only process that ever holds mail passwords.

The chat and `serve` start this file per operation as
`python -I -S <absolute path of this file>` (no PYTHON* variables, no current
directory, no user site, no .pth files) with a minimal environment, send one JSON
request on stdin and read one JSON response on stdout. Only this process opens
the connector config file (PA_MAIL_CONFIG) and only this module imports imaplib
and ssl for mail, so no Claude Code process or child can ever inherit a password.

IMAP is read-only, with one narrow exception (`draft`, below):

- implicit TLS only (IMAP4_SSL, verified certificate and host name, TLS >= 1.2);
  there is no STARTTLS code path, so nothing can be downgraded to plain text;
- EXAMINE instead of SELECT, and only BODY.PEEK[...] fetches, so nothing is
  marked as read; STORE, COPY, MOVE, EXPUNGE, CREATE, DELETE and friends are
  refused by `GuardedIMAP` before anything is sent (a refusal is a bug in our
  code, reported as `protocol`);
- every literal and the whole session are byte-capped;
- each FETCH response is parsed on its own and attributed by its UID item, never
  by position, so an unsolicited response for another message cannot lend its
  sender to the wrong mail.

The one write is `draft`: for an account with "allow_draft_append": true it builds a
plain-text message from our own fields (From is always the account's address) and
APPENDs it with the \\Draft flag to the configured drafts folder, after LIST shows
that folder exists. That session's guard allows LIST and APPEND on top of the read
commands and nothing else; nothing is ever sent, moved or deleted. The person sends
the draft from their own mail program.

Errors are fixed codes. Exceptions are reported by type name only: the text of an
imaplib or ssl exception can contain server text or host names. imaplib's
command log (which would hold the LOGIN line) is disabled.
"""

from __future__ import annotations

import os
import sys

if __name__ == "__main__" and not __package__:  # pragma: no cover - exercised through subprocess tests
    # -I drops the script directory from sys.path. The package root is appended (never
    # inserted): the standard library always wins over anything that sits next to the
    # package, e.g. an old backport or a planted ssl.py in site-packages.
    _ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _ROOT not in sys.path:
        sys.path.append(_ROOT)

import imaplib  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import socket  # noqa: E402
import ssl  # noqa: E402
import stat  # noqa: E402
import sysconfig  # noqa: E402
import time  # noqa: E402
from datetime import date, timedelta  # noqa: E402
from pathlib import Path  # noqa: E402

from personal_agent.mailconfig import (  # noqa: E402
    ADDRESS_RE,
    ConnectorConfig,
    MailAccount,
    MailConfigError,
    imap_utf7,
    load_connector_config,
    quote_astring,
    quote_mailbox,
)
from personal_agent.mailparse import (  # noqa: E402
    MESSAGE_ID_RE,
    UNREADABLE,
    _same_site,
    clean_block,
    clean_line,
    domain_of,
    extract_body,
    imap_date,
    injection_signals,
    parse_headers,
    parse_internaldate,
)

PROTOCOL = 1
MAX_REQUEST = 256 * 1024
MAX_LITERAL = 1024 * 1024 + 4096
MAX_SESSION_BYTES = 32 * 1024 * 1024
MAX_CA_BYTES = 1024 * 1024
MAX_DEADLINE_S = 180.0
OP_TIMEOUT_S = 20.0
MIN_ACCOUNT_SHARE_S = 15.0
FETCH_BATCH = 25
MAX_BODY_ITEMS = 30
MAX_BODY_CHARS = 20_000
MAX_PER_FOLDER = 100
MAX_DAYS = 60
BODY_PEEK_BYTES = 262_144
MAX_PAREN_DEPTH = 16

READ_MODE = frozenset({"CAPABILITY", "LOGIN", "STATUS", "EXAMINE", "UID", "NOOP", "LOGOUT"})
APPEND_MODE = READ_MODE | {"LIST", "APPEND"}
MODES = {"read": READ_MODE, "append": APPEND_MODE}

ERROR_CODES = frozenset((
    "config", "connect", "tls", "timeout", "auth", "login_disabled", "folder_missing", "protocol", "too_large",
    "append_disabled", "drafts_folder_missing", "mismatch", "http_status", "redirect_refused", "not_calendar",
    "internal", "bad_request",
))
COMMANDS = ("describe", "check", "scan", "bodies", "draft")
MAX_DRAFT_CHARS = 20_000

SCAN_FIELDS = "FROM REPLY-TO TO CC SUBJECT DATE MESSAGE-ID LIST-ID LIST-UNSUBSCRIBE PRECEDENCE AUTO-SUBMITTED"
SCAN_ITEMS = f"(UID FLAGS INTERNALDATE RFC822.SIZE BODY.PEEK[HEADER.FIELDS ({SCAN_FIELDS})])"
BODY_FIELDS = "FROM REPLY-TO TO CC SUBJECT DATE MESSAGE-ID IN-REPLY-TO"
BODY_ITEMS = f"(UID RFC822.SIZE BODY.PEEK[HEADER.FIELDS ({BODY_FIELDS})] BODY.PEEK[]<0.{BODY_PEEK_BYTES}>)"

# BODY[...] and BINARY[...] without .PEEK, and RFC822 / RFC822.TEXT / RFC822.HEADER, may set \Seen.
_FETCH_FORBIDDEN = re.compile(r"\b(?:BODY|BINARY)\[|\bRFC822(?!\.SIZE\b)", re.IGNORECASE)
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_STDLIB_MODULES = ("ssl", "imaplib", "email", "json", "urllib", "socket", "html")


class WorkerFailure(Exception):
    """An expected failure with a fixed code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code if code in ERROR_CODES else "internal"


class ProtocolGuard(Exception):
    """Our own code tried a command the read-only guard does not allow (a bug, never sent)."""


class BudgetExceeded(Exception):
    """A literal or the whole session is larger than its cap."""


class DeadlineExceeded(Exception):
    pass


class FetchSyntax(Exception):
    pass


# --- process hygiene -------------------------------------------------------------------


def harden_process() -> None:
    """No core dumps, private files only, and (Linux) not ptrace-able or /proc-readable by
    other processes of the same user."""
    os.umask(0o077)
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ImportError, ValueError, OSError):
        pass
    if sys.platform.startswith("linux"):
        try:
            import ctypes

            libc = ctypes.CDLL(None, use_errno=True)
            libc.prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE = 4
        except (OSError, AttributeError):
            pass


def stdlib_problem() -> str | None:
    """Why one of the security-relevant modules was not loaded from the standard library."""
    roots = {os.path.realpath(p) for p in (sysconfig.get_paths().get("stdlib"),
                                           sysconfig.get_paths().get("platstdlib")) if p}
    for name in _STDLIB_MODULES:
        module = sys.modules.get(name)
        path = getattr(module, "__file__", None)
        if module is None or path is None:
            continue
        real = os.path.realpath(path)
        if not any(real == root or real.startswith(root + os.sep) for root in roots):
            return f"{name} is not the standard library module"
    return None


# --- time ------------------------------------------------------------------------------


class Deadline:
    def __init__(self, end: float, clock=time.monotonic) -> None:
        self.end = end
        self.clock = clock

    def remaining(self) -> float:
        return self.end - self.clock()

    def expired(self) -> bool:
        return self.remaining() <= 0

    def check(self) -> None:
        if self.expired():
            raise DeadlineExceeded()

    def op_timeout(self) -> float:
        self.check()
        return max(0.5, min(OP_TIMEOUT_S, self.remaining()))

    def share(self, seconds: float) -> "Deadline":
        return Deadline(min(self.end, self.clock() + seconds), self.clock)


# --- TLS -------------------------------------------------------------------------------


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def read_ca_file(path: str | None, forbidden_roots: tuple[str, ...]) -> bytes | None:
    """The PEM bundle of a private CA, read once from a file only this user can change.

    ca_file replaces the system trust store, so whoever can swap it can read every
    password and mail: it must be a regular file (no symlink) owned by this user, not
    group/other-writable, at most 1 MiB, and outside the vault and the state directory.
    """
    if path is None:
        return None
    real = os.path.realpath(path)
    for root in forbidden_roots:
        if _inside(real, os.path.realpath(root)) or _inside(os.path.abspath(path), os.path.abspath(root)):
            raise MailConfigError("ca_file must not be inside the vault or the state directory")
    try:
        fd = os.open(path, _READ_FLAGS)
    except OSError:
        raise MailConfigError("ca_file must be a regular file owned by you") from None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or info.st_size > MAX_CA_BYTES
                or (hasattr(os, "getuid") and info.st_uid != os.getuid())):
            raise MailConfigError("ca_file must be a regular file owned by you, not writable by others")
        data = os.read(fd, MAX_CA_BYTES + 1)
    except OSError:
        raise MailConfigError("ca_file cannot be read") from None
    finally:
        os.close(fd)
    if len(data) > MAX_CA_BYTES or b"-----BEGIN CERTIFICATE-----" not in data:
        raise MailConfigError("ca_file must be a PEM certificate bundle")
    return data


def tls_context(ca_data: bytes | None) -> ssl.SSLContext:
    """Verified TLS >= 1.2. With ca_data, only that bundle is trusted. Never weakened."""
    if ca_data is not None:
        try:
            context = ssl.create_default_context(cadata=ca_data.decode("ascii"))
        except (ssl.SSLError, ValueError, UnicodeDecodeError):
            raise MailConfigError("ca_file must be a PEM certificate bundle") from None
    else:
        context = ssl.create_default_context()
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


# --- the guarded IMAP client -----------------------------------------------------------


def check_command(allowed: frozenset[str], name: str, args: tuple) -> None:
    """Refuse any command outside the mode, any UID sub-command but SEARCH/FETCH, and any
    FETCH item that could set \\Seen. Raises ProtocolGuard; nothing is sent."""
    upper = str(name).upper()
    if upper not in allowed:
        raise ProtocolGuard(upper)
    if upper == "UID":
        texts = [a.decode("ascii", "replace") if isinstance(a, bytes) else str(a) for a in args if a is not None]
        sub = texts[0].upper() if texts else ""
        if sub not in ("SEARCH", "FETCH"):
            raise ProtocolGuard(f"UID {sub}")
        if sub == "FETCH" and _FETCH_FORBIDDEN.search(" ".join(texts[2:])):
            raise ProtocolGuard("FETCH without PEEK")


class GuardedIMAP(imaplib.IMAP4_SSL):
    """imaplib over implicit TLS with a command allowlist and byte caps."""

    def __init__(self, host: str, port: int, *, context: ssl.SSLContext, timeout_s: float, mode: str) -> None:
        if mode not in MODES:
            raise ValueError("unknown mode")
        # IMAP4.__init__ already sends CAPABILITY: the guard and the budget come first.
        self._pa_allowed = MODES[mode]
        self._pa_budget = MAX_SESSION_BYTES
        super().__init__(host, port, ssl_context=context, timeout=timeout_s)

    def _command(self, name, *args):  # noqa: ANN001 - imaplib's signature
        check_command(self._pa_allowed, name, args)
        return super()._command(name, *args)

    def _log(self, line):  # noqa: ANN001 - never keep a command log (it would hold LOGIN)
        pass

    def print_log(self):  # noqa: D102
        pass

    def _spend(self, count: int) -> None:
        self._pa_budget -= count
        if self._pa_budget < 0:
            raise BudgetExceeded()

    def read(self, size):  # noqa: ANN001
        if size < 0 or size > MAX_LITERAL:
            raise BudgetExceeded()
        self._spend(size)
        return super().read(size)

    def readline(self):  # noqa: D102
        line = super().readline()
        self._spend(len(line))
        return line

    def set_timeout(self, seconds: float) -> None:
        try:
            self.sock.settimeout(seconds)
        except OSError:
            pass


def error_code(exc: BaseException) -> str:
    """A fixed code for an exception; its text is never used."""
    if isinstance(exc, WorkerFailure):
        return exc.code
    if isinstance(exc, MailConfigError):
        return "config"
    if isinstance(exc, (DeadlineExceeded, socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(exc, BudgetExceeded):
        return "too_large"
    if isinstance(exc, (ProtocolGuard, FetchSyntax)):
        return "protocol"
    if isinstance(exc, (ssl.SSLError, ssl.CertificateError)):
        return "tls"
    if isinstance(exc, imaplib.IMAP4.error):
        return "protocol"
    if isinstance(exc, OSError):
        return "connect"
    if isinstance(exc, (ValueError, UnicodeError)):
        return "protocol"
    return "internal"


def connect(account: MailAccount, context: ssl.SSLContext, deadline: Deadline, mode: str = "read") -> GuardedIMAP:
    """Connect, check capabilities and log in. Raises WorkerFailure or a mapped exception."""
    imap = GuardedIMAP(account.host, account.port, context=context, timeout_s=deadline.op_timeout(), mode=mode)
    try:
        capabilities = tuple(str(c).upper() for c in getattr(imap, "capabilities", ()))
        if "IMAP4REV1" not in capabilities:
            raise WorkerFailure("protocol")
        if "LOGINDISABLED" in capabilities:
            raise WorkerFailure("login_disabled")
        imap.set_timeout(deadline.op_timeout())
        try:
            imap.login(quote_astring(account.user), account.password)
        except imaplib.IMAP4.abort:
            raise WorkerFailure("protocol") from None
        except imaplib.IMAP4.error:
            raise WorkerFailure("auth") from None
    except BaseException:
        close_quietly(imap)
        raise
    return imap


def close_quietly(imap: GuardedIMAP | None) -> None:
    if imap is None:
        return
    try:
        if imap.state != "LOGOUT":
            imap.set_timeout(3.0)
            imap.logout()
    except Exception:  # noqa: BLE001 - LOGOUT errors never matter
        pass
    try:
        imap.shutdown()
    except Exception:  # noqa: BLE001
        pass


# --- response parsing ------------------------------------------------------------------


def _tokens(text: bytes, out: list) -> None:
    i, n = 0, len(text)
    while i < n:
        c = text[i:i + 1]
        if c in (b" ", b"\t"):
            i += 1
        elif c in (b"(", b")"):
            out.append(c.decode("ascii"))
            i += 1
        elif c == b'"':
            j = i + 1
            buf = bytearray()
            while j < n:
                ch = text[j:j + 1]
                if ch == b"\\" and j + 1 < n:
                    buf += text[j + 1:j + 2]
                    j += 2
                    continue
                if ch == b'"':
                    break
                buf += ch
                j += 1
            else:
                raise FetchSyntax("open quote")
            out.append(("str", bytes(buf)))
            i = j + 1
        elif c == b"{":
            if not re.fullmatch(rb"\{\d+\}", text[i:]):
                raise FetchSyntax("literal marker")
            out.append(("litmark",))
            i = n
        else:
            j = i
            while j < n:
                ch = text[j:j + 1]
                if ch == b"[":
                    k = text.find(b"]", j)
                    if k < 0:
                        raise FetchSyntax("open bracket")
                    j = k + 1
                    continue
                if ch in (b" ", b"(", b")", b'"', b"{"):
                    break
                j += 1
            atom = text[i:j]
            out.append(("nil",) if atom.upper() == b"NIL" else ("atom", atom))
            i = j


def _response_tokens(segments: list[tuple[bytes, bytes | None]]) -> list:
    tokens: list = []
    for text, literal in segments:
        _tokens(text, tokens)
        if literal is not None:
            if not tokens or tokens[-1] != ("litmark",):
                raise FetchSyntax("literal without marker")
            tokens[-1] = ("lit", literal)
        elif tokens and tokens[-1] == ("litmark",):
            raise FetchSyntax("marker without literal")
    return tokens


def _value(tokens: list, pos: int, depth: int = 0) -> tuple[object, int]:
    if depth > MAX_PAREN_DEPTH:
        raise FetchSyntax("too deep")
    if pos >= len(tokens):
        raise FetchSyntax("truncated")
    token = tokens[pos]
    if token == "(":
        items: list = []
        pos += 1
        while True:
            if pos >= len(tokens):
                raise FetchSyntax("open list")
            if tokens[pos] == ")":
                return items, pos + 1
            item, pos = _value(tokens, pos, depth + 1)
            items.append(item)
    if token == ")":
        raise FetchSyntax("unexpected )")
    kind = token[0]
    if kind in ("str", "lit"):
        return token[1], pos + 1
    if kind == "nil":
        return None, pos + 1
    if kind == "atom":
        return token[1].decode("ascii", "replace"), pos + 1
    raise FetchSyntax("unexpected token")


def _item_key(name: str) -> str | None:
    upper = name.upper()
    if upper.startswith("BODY[HEADER"):
        return "HEADER"
    if upper.startswith("BODY[]"):
        return "BODY"
    if upper in ("UID", "FLAGS", "INTERNALDATE", "RFC822.SIZE"):
        return upper
    return None


def parse_fetch_response(segments: list[tuple[bytes, bytes | None]]) -> dict[str, object]:
    """The items of one untagged FETCH response ('<seq> (<item> <value> ...)')."""
    tokens = _response_tokens(segments)
    if len(tokens) < 3 or tokens[0][0] != "atom" or not tokens[0][1].isdigit() or tokens[1] != "(":
        raise FetchSyntax("not a FETCH response")
    items: dict[str, object] = {}
    pos = 2
    while True:
        if pos >= len(tokens):
            raise FetchSyntax("open FETCH")
        if tokens[pos] == ")":
            break
        name = tokens[pos]
        if not isinstance(name, tuple) or name[0] != "atom":
            raise FetchSyntax("item name")
        value, pos = _value(tokens, pos + 1)
        key = _item_key(name[1].decode("ascii", "replace"))
        if key is not None:
            if key in items:
                raise FetchSyntax("item twice")
            items[key] = value
    return items


def group_fetch(data: list) -> list[list[tuple[bytes, bytes | None]]]:
    """imaplib's flat FETCH data regrouped into one segment list per response.

    A response starts with '<seq> ('; the text that follows a literal starts with a
    space or ')' and belongs to the response before it.
    """
    responses: list[list[tuple[bytes, bytes | None]]] = []
    for element in data or ():
        if isinstance(element, tuple) and len(element) == 2 and isinstance(element[0], bytes) \
                and isinstance(element[1], bytes):
            text, literal = element
        elif isinstance(element, bytes):
            text, literal = element, None
        else:
            continue
        if re.match(rb"\d+ \(", text):
            responses.append([(text, literal)])
        elif responses:
            responses[-1].append((text, literal))
    return responses


def collect_fetch(data: list, requested: set[int]) -> tuple[dict[int, dict], set[int]]:
    """{uid: items} for requested UIDs only, and the UIDs with conflicting data.

    Responses without a requested UID item (unsolicited flag updates, other messages)
    are ignored; nothing is ever paired by position. Two responses for one UID are
    merged only when they do not disagree.
    """
    found: dict[int, dict] = {}
    conflicts: set[int] = set()
    for segments in group_fetch(data):
        try:
            items = parse_fetch_response(segments)
        except FetchSyntax:
            continue
        uid_text = items.get("UID")
        if not isinstance(uid_text, str) or not uid_text.isdigit():
            continue
        uid = int(uid_text)
        if uid not in requested:
            continue
        if uid in found:
            for key, value in items.items():
                if key in found[uid] and found[uid][key] != value:
                    conflicts.add(uid)
                found[uid].setdefault(key, value)
        else:
            found[uid] = items
    return found, conflicts


def parse_status(data: list, folder: str) -> dict[str, int]:
    """MESSAGES/UNSEEN/UIDNEXT/UIDVALIDITY of the STATUS response for folder."""
    wanted = imap_utf7(folder)
    candidates: list[tuple[bool, dict[str, int]]] = []
    literal_name: str | None = None
    for element in data or ():
        if isinstance(element, tuple) and len(element) == 2 and isinstance(element[1], bytes):
            literal_name = element[1].decode("ascii", "replace")  # the mailbox came as a literal
            continue
        if not isinstance(element, bytes):
            continue
        match = re.search(rb"\(([A-Za-z0-9 ]*)\)\s*$", element)
        if match is None:
            literal_name = None
            continue
        head = element[: match.start()].strip()
        if not head and literal_name is not None:
            mailbox = literal_name
        else:
            tokens: list = []
            try:
                _tokens(head, tokens)
            except FetchSyntax:
                tokens = []
            first = tokens[0] if len(tokens) == 1 else None
            mailbox = first[1].decode("ascii", "replace") if isinstance(first, tuple) and len(first) > 1 else ""
        literal_name = None
        words = match.group(1).split()
        counts: dict[str, int] = {}
        for key, value in zip(words[0::2], words[1::2]):
            if value.isdigit() and len(value) <= 12:
                counts[key.decode("ascii").upper()] = int(value)
        candidates.append((mailbox == wanted, counts))
    for matched, counts in candidates:
        if matched:
            return counts
    if len(candidates) == 1:
        return candidates[0][1]
    raise FetchSyntax("no STATUS response")


def parse_search(data: list) -> list[int]:
    uids: set[int] = set()
    for element in data or ():
        if element is None:
            continue
        if not isinstance(element, bytes):
            raise FetchSyntax("SEARCH")
        for word in element.split():
            if not word.isdigit() or len(word) > 10:
                raise FetchSyntax("SEARCH")
            value = int(word)
            if 0 < value < 2 ** 32:
                uids.add(value)
    return sorted(uids)


# --- records ---------------------------------------------------------------------------


def _flags(value: object) -> tuple[bool, bool]:
    names = {str(f).lower() for f in value} if isinstance(value, list) else set()
    return "\\seen" in names, "\\answered" in names


def _int_item(value: object) -> int:
    return int(value) if isinstance(value, str) and value.isdigit() and len(value) <= 12 else 0


def message_record(uid: int, items: dict) -> dict:
    """The allowlisted, cleaned summary of one message. A message that cannot be parsed
    becomes an '(unreadable)' record; the rest of the folder continues."""
    seen, answered = _flags(items.get("FLAGS"))
    internal = items.get("INTERNALDATE")
    record = {
        "uid": uid, "date": parse_internaldate(internal if isinstance(internal, bytes) else None),
        "flags": {"seen": seen, "answered": answered}, "size": _int_item(items.get("RFC822.SIZE")),
        "from_name": "", "from_addr": None, "subject": UNREADABLE, "list": False, "auto": False,
        "to_count": 0, "cc_count": 0,
    }
    header = items.get("HEADER")
    try:
        fields = parse_headers(header if isinstance(header, bytes) else b"")
    except Exception:  # noqa: BLE001 - one hostile message must not stop the folder
        return record
    sender = fields["from"]
    record.update(
        from_name=sender.name, from_addr=sender.addr, subject=fields["subject"], list=fields["list"],
        auto=fields["auto"], to_count=fields["to_count"], cc_count=fields["cc_count"],
    )
    if record["date"] is None:
        record["date"] = fields["date"]
    return record


# --- commands --------------------------------------------------------------------------


def _uidvalidity(imap: GuardedIMAP) -> int | None:
    values = imap.untagged_responses.get("UIDVALIDITY") or []
    for value in reversed(values):
        if isinstance(value, bytes) and value.strip().isdigit() and len(value.strip()) <= 12:
            return int(value.strip())
    return None


def _examine(imap: GuardedIMAP, folder: str, deadline: Deadline) -> int:
    imap.set_timeout(deadline.op_timeout())
    typ, _ = imap.select(quote_mailbox(folder), readonly=True)
    if typ != "OK":
        raise WorkerFailure("folder_missing")
    uidvalidity = _uidvalidity(imap)
    if uidvalidity is None:
        raise WorkerFailure("protocol")
    return uidvalidity


def scan_folder(imap: GuardedIMAP, folder: str, deadline: Deadline, days: int, max_per_folder: int,
                today: date) -> dict:
    result: dict = {"uidvalidity": None, "uidnext": None, "messages": None, "unseen": None, "error": None,
                    "records": [], "skipped": 0}
    imap.set_timeout(deadline.op_timeout())
    typ, data = imap.status(quote_mailbox(folder), "(MESSAGES UNSEEN UIDNEXT UIDVALIDITY)")
    if typ != "OK":
        result["error"] = "folder_missing"
        return result
    counts = parse_status(data, folder)
    result.update(uidnext=counts.get("UIDNEXT"), messages=counts.get("MESSAGES"), unseen=counts.get("UNSEEN"))
    uidvalidity = _examine(imap, folder, deadline)
    result["uidvalidity"] = uidvalidity
    imap.set_timeout(deadline.op_timeout())
    typ, data = imap.uid("SEARCH", "SINCE", imap_date(today - timedelta(days=days)))
    if typ != "OK":
        raise WorkerFailure("protocol")
    uids = parse_search(data)[-max_per_folder:]
    records: list[dict] = []
    for start in range(0, len(uids), FETCH_BATCH):
        batch = uids[start:start + FETCH_BATCH]
        imap.set_timeout(deadline.op_timeout())
        typ, data = imap.uid("FETCH", ",".join(str(u) for u in batch), SCAN_ITEMS)
        if typ != "OK":
            raise WorkerFailure("protocol")
        found, conflicts = collect_fetch(data, set(batch))
        for uid in batch:
            if uid not in found or uid in conflicts:
                result["skipped"] += 1
                continue
            records.append(message_record(uid, found[uid]))
    result["records"] = records
    return result


def scan_account(account: MailAccount, deadline: Deadline, days: int, max_per_folder: int, today: date,
                 forbidden_roots: tuple[str, ...]) -> dict:
    result: dict = {"name": account.name, "error": None, "folders": {}}
    imap = None
    try:
        context = tls_context(read_ca_file(account.ca_file, forbidden_roots))
        imap = connect(account, context, deadline)
        for folder in account.folders:
            try:
                result["folders"][folder] = scan_folder(imap, folder, deadline, days, max_per_folder, today)
            except (WorkerFailure, FetchSyntax) as exc:
                code = error_code(exc)
                if code not in ("folder_missing", "protocol"):
                    raise
                result["folders"][folder] = {"uidvalidity": None, "uidnext": None, "messages": None,
                                             "unseen": None, "error": code, "records": [], "skipped": 0}
    except Exception as exc:  # noqa: BLE001 - mapped to a fixed code; other accounts continue
        result["error"] = error_code(exc)
    finally:
        close_quietly(imap)
    return result


def _bodies_for_account(account: MailAccount, items: list[dict], deadline: Deadline, body_chars: int,
                        forbidden_roots: tuple[str, ...]) -> list[dict]:
    out: list[dict] = []
    imap = None

    def failed(item: dict, code: str) -> dict:
        return {"ref_key": [item["account"], item["folder"], item["uidvalidity"], item["uid"]], "error": code}

    try:
        context = tls_context(read_ca_file(account.ca_file, forbidden_roots))
        imap = connect(account, context, deadline)
    except Exception as exc:  # noqa: BLE001
        close_quietly(imap)
        return [failed(item, error_code(exc)) for item in items]
    try:
        selected: tuple[str, int] | None = None
        for index, item in enumerate(items):
            try:
                if selected is None or selected[0] != item["folder"]:
                    selected = (item["folder"], _examine(imap, item["folder"], deadline))
                if selected[1] != item["uidvalidity"]:
                    out.append(failed(item, "mismatch"))
                    continue
                imap.set_timeout(deadline.op_timeout())
                typ, data = imap.uid("FETCH", str(item["uid"]), BODY_ITEMS)
                if typ != "OK":
                    raise WorkerFailure("protocol")
                found, conflicts = collect_fetch(data, {item["uid"]})
                if item["uid"] not in found or item["uid"] in conflicts:
                    out.append(failed(item, "protocol"))
                    continue
                out.append(body_record(item, found[item["uid"]], body_chars, deadline))
            except (WorkerFailure, FetchSyntax) as exc:
                out.append(failed(item, error_code(exc)))
                if error_code(exc) == "folder_missing":
                    selected = None
            except Exception as exc:  # noqa: BLE001 - the connection is unusable now
                code = error_code(exc)
                out.extend(failed(rest, code) for rest in items[index:])
                break
    finally:
        close_quietly(imap)
    return out


def body_record(item: dict, items: dict, body_chars: int, deadline: Deadline) -> dict:
    """What the parent may see of one message: cleaned headers and bounded, inert text."""
    ref_key = [item["account"], item["folder"], item["uidvalidity"], item["uid"]]
    header = items.get("HEADER")
    raw = items.get("BODY")
    size = _int_item(items.get("RFC822.SIZE"))
    try:
        fields = parse_headers(header if isinstance(header, bytes) else b"")
    except Exception:  # noqa: BLE001
        fields = None
    try:
        body = extract_body(raw if isinstance(raw, bytes) else b"", max_chars=body_chars,
                            declared_size=size or None, deadline=deadline.expired)
    except Exception:  # noqa: BLE001
        body = None
    if fields is None:
        return {"ref_key": ref_key, "from_name": "", "from_addr": None, "reply_to_addr": None,
                "subject": UNREADABLE, "date": None, "message_id_ok": False, "text": "(could not read this mail)",
                "source": "none", "truncated": False, "attachments": 0, "signals": []}
    sender, reply_to = fields["from"], fields["reply_to"]
    extra = list(body.signals) if body is not None else []
    from_domain, reply_domain = domain_of(sender.addr), domain_of(reply_to.addr)
    if reply_domain and (not from_domain or not _same_site(reply_domain, from_domain)):
        extra.append("reply_to_differs")
    if reply_to.idn:
        extra.append("idn")
    text = body.text if body is not None else "(could not read this mail)"
    signals = injection_signals(f"{fields['subject']}\n{text}", body.hidden_dropped if body else 0, 0, sender,
                                tuple(extra))
    return {
        "ref_key": ref_key, "from_name": sender.name, "from_addr": sender.addr, "reply_to_addr": reply_to.addr,
        "subject": fields["subject"], "date": fields["date"], "message_id_ok": fields["message_id_ok"],
        "text": text, "source": body.source if body is not None else "none",
        "truncated": body.truncated if body is not None else False,
        "attachments": body.attachments if body is not None else 0, "signals": list(signals),
    }


def _forbidden_roots(request: dict) -> tuple[str, ...]:
    roots = request.get("forbidden_roots", [])
    if not isinstance(roots, list) or len(roots) > 10:
        raise WorkerFailure("bad_request")
    if not all(isinstance(r, str) and os.path.isabs(r) and "\0" not in r for r in roots):
        raise WorkerFailure("bad_request")
    return tuple(roots)


def _int_field(request: dict, key: str, low: int, high: int, default: int) -> int:
    value = request.get(key, default)
    if type(value) is not int or not low <= value <= high:
        raise WorkerFailure("bad_request")
    return value


def _describe(config: ConnectorConfig) -> dict:
    return {
        "accounts": [{"name": a.name, "folders": list(a.folders), "has_address": a.address is not None,
                      "allow_draft_append": a.allow_draft_append, "drafts_folder": a.drafts_folder}
                     for a in config.accounts],
        "calendars": [{"name": c.name} for c in config.calendars],
    }


def _check(request: dict, config: ConnectorConfig, deadline: Deadline) -> dict:
    roots = _forbidden_roots(request)
    connect_now = request.get("connect", False) is True
    results: list[dict] = []
    for account in config.accounts:
        code = "ok"
        imap = None
        try:
            context = tls_context(read_ca_file(account.ca_file, roots))
            if connect_now:
                share = deadline.share(max(MIN_ACCOUNT_SHARE_S, deadline.remaining() / len(config.accounts)))
                imap = connect(account, context, share)
                for folder in account.folders:
                    imap.set_timeout(share.op_timeout())
                    typ, _ = imap.status(quote_mailbox(folder), "(MESSAGES)")
                    if typ != "OK":
                        code = "folder_missing"
                        break
        except Exception as exc:  # noqa: BLE001
            code = error_code(exc)
        finally:
            close_quietly(imap)
        results.append({"account": account.name, "code": code})
    calendars = [{"calendar": c.name, "code": "not_checked"} for c in config.calendars]
    errors = [{"account": r["account"], "code": r["code"]} for r in results if r["code"] != "ok"]
    return {"ok": not errors, "errors": errors, "accounts": results, "calendars": calendars}


def _scan(request: dict, config: ConnectorConfig, deadline: Deadline, today: date) -> dict:
    roots = _forbidden_roots(request)
    days = _int_field(request, "days", 1, MAX_DAYS, 7)
    per_folder = _int_field(request, "max_per_folder", 1, MAX_PER_FOLDER, 50)
    names = request.get("accounts")
    if names is None:
        accounts = list(config.accounts)
    else:
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise WorkerFailure("bad_request")
        accounts = [a for a in config.accounts if a.name in names]
    results: list[dict] = []
    share = max(MIN_ACCOUNT_SHARE_S, deadline.remaining() / max(1, len(accounts)))
    for account in accounts:
        if deadline.expired():
            results.append({"name": account.name, "error": "timeout", "folders": {}})
            continue
        results.append(scan_account(account, deadline.share(share), days, per_folder, today, roots))
    errors = [{"account": r["name"], "code": r["error"]} for r in results if r["error"]]
    return {"ok": not errors, "errors": errors, "accounts": results}


def _bodies(request: dict, config: ConnectorConfig, deadline: Deadline) -> dict:
    roots = _forbidden_roots(request)
    body_chars = _int_field(request, "body_chars", 100, MAX_BODY_CHARS, 4000)
    raw_items = request.get("items")
    if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= MAX_BODY_ITEMS:
        raise WorkerFailure("bad_request")
    grouped: dict[str, list[dict]] = {}
    for raw in raw_items:
        if not isinstance(raw, dict) or set(raw) != {"account", "folder", "uidvalidity", "uid"}:
            raise WorkerFailure("bad_request")
        account = config.account(raw["account"]) if isinstance(raw["account"], str) else None
        if account is None or raw["folder"] not in account.folders:
            raise WorkerFailure("bad_request")
        for key in ("uidvalidity", "uid"):
            if type(raw[key]) is not int or not 0 < raw[key] < 2 ** 32:
                raise WorkerFailure("bad_request")
        grouped.setdefault(account.name, []).append(raw)
    records: list[dict] = []
    for name, items in grouped.items():
        account = config.account(name)
        assert account is not None
        records.extend(_bodies_for_account(account, sorted(items, key=lambda i: (i["folder"], i["uid"])),
                                           deadline, body_chars, roots))
    errors = [{"account": r["ref_key"][0], "code": r["error"]} for r in records if "error" in r]
    return {"ok": not errors, "errors": errors, "items": records}


def _draft_message(account: MailAccount, request: dict) -> bytes:
    """The draft as RFC 5322 bytes, built only from validated fields."""
    from email.message import EmailMessage
    from email.utils import formatdate, make_msgid

    to = request.get("to")
    subject = request.get("subject")
    body = request.get("body")
    reply_to_id = request.get("in_reply_to")
    if not isinstance(to, str) or len(to) > 254 or not ADDRESS_RE.fullmatch(to):
        raise WorkerFailure("bad_request")
    if not isinstance(subject, str) or not isinstance(body, str) or len(body) > MAX_DRAFT_CHARS:
        raise WorkerFailure("bad_request")
    if reply_to_id is not None and (not isinstance(reply_to_id, str) or not MESSAGE_ID_RE.fullmatch(reply_to_id)):
        raise WorkerFailure("bad_request")
    text, _ = clean_block(body, MAX_DRAFT_CHARS)
    msg = EmailMessage()
    msg["From"] = account.address
    msg["To"] = to
    msg["Subject"] = clean_line(subject, 200)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=domain_of(account.address) or None)
    if reply_to_id:
        msg["In-Reply-To"] = reply_to_id
        msg["References"] = reply_to_id
    msg.set_content(text or "\n")
    return bytes(msg).replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


def _draft(request: dict, config: ConnectorConfig, deadline: Deadline) -> dict:
    roots = _forbidden_roots(request)
    name = request.get("account")
    account = config.account(name) if isinstance(name, str) else None
    if account is None:
        raise WorkerFailure("bad_request")
    if not account.allow_draft_append or not account.drafts_folder or not account.address:
        raise WorkerFailure("append_disabled")
    raw = _draft_message(account, request)
    imap = None
    try:
        context = tls_context(read_ca_file(account.ca_file, roots))
        imap = connect(account, context, deadline, mode="append")
        imap.set_timeout(deadline.op_timeout())
        typ, data = imap.list('""', quote_mailbox(account.drafts_folder))
        if typ != "OK" or not any(isinstance(d, bytes) and d.strip() for d in data or ()):
            raise WorkerFailure("drafts_folder_missing")
        imap.set_timeout(deadline.op_timeout())
        typ, _ = imap.append(quote_mailbox(account.drafts_folder), "(\\Draft)", None, raw)
        if typ != "OK":
            raise WorkerFailure("protocol")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "errors": [{"account": account.name, "code": error_code(exc)}]}
    finally:
        close_quietly(imap)
    return {"ok": True, "errors": [], "account": account.name, "folder": account.drafts_folder}


def handle(request: dict, config: ConnectorConfig, now: float, *, today: date | None = None,
           clock=time.monotonic) -> dict:
    """One request -> one response dict (protocol, ok, errors, ...)."""
    try:
        if not isinstance(request, dict) or request.get("protocol") != PROTOCOL:
            raise WorkerFailure("bad_request")
        command = request.get("command")
        if command not in COMMANDS:
            raise WorkerFailure("bad_request")
        seconds = request.get("deadline_s", 45)
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 < seconds <= MAX_DEADLINE_S:
            raise WorkerFailure("bad_request")
        deadline = Deadline(now + float(seconds), clock)
        if command == "describe":
            body = {"ok": True, "errors": [], **_describe(config)}
        elif command == "check":
            body = _check(request, config, deadline)
        elif command == "scan":
            body = _scan(request, config, deadline, today or date.today())
        elif command == "draft":
            body = _draft(request, config, deadline)
        else:
            body = _bodies(request, config, deadline)
    except WorkerFailure as exc:
        body = {"ok": False, "errors": [{"code": exc.code}]}
    except Exception as exc:  # noqa: BLE001 - type name only, never the text
        body = {"ok": False, "errors": [{"code": "internal", "type": type(exc).__name__}]}
    return {"protocol": PROTOCOL, **body}


def main() -> int:
    """Read one request from stdin, write one response to stdout, exit 0. Never raises."""
    try:
        harden_process()
        problem = stdlib_problem()
        if problem:
            response = {"protocol": PROTOCOL, "ok": False, "errors": [{"code": "internal", "type": "stdlib"}]}
        else:
            raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
            if len(raw) > MAX_REQUEST:
                raise WorkerFailure("bad_request")
            try:
                request = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise WorkerFailure("bad_request") from None
            path = os.environ.get("PA_MAIL_CONFIG", "")
            if not path or not os.path.isabs(path):
                raise WorkerFailure("config")
            try:
                config = load_connector_config(Path(path))
            except MailConfigError as exc:
                # Key paths only (never a value): safe to show the person.
                response = {"protocol": PROTOCOL, "ok": False, "errors": [{"code": "config", "detail": str(exc)}]}
            else:
                response = handle(request, config, time.monotonic())
    except WorkerFailure as exc:
        response = {"protocol": PROTOCOL, "ok": False, "errors": [{"code": exc.code}]}
    except BaseException as exc:  # noqa: BLE001 - type name only
        response = {"protocol": PROTOCOL, "ok": False, "errors": [{"code": "internal", "type": type(exc).__name__}]}
    try:
        sys.stdout.buffer.write(json.dumps(response, ensure_ascii=True, sort_keys=True).encode("ascii"))
        sys.stdout.buffer.flush()
    except BaseException:  # noqa: BLE001 - the parent sees an empty answer
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover - run as a subprocess
    sys.exit(main())
