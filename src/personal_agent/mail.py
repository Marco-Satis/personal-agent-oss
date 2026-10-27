"""Mail in the chat and on Telegram: tier-1 `mail` and `mail show <id>`, without any model.

This module runs in the chat and `serve` processes. It never holds a mail password:
`WorkerClient` starts `mail_worker.py` per operation as a separate process
(`python -I -S`, minimal environment, one JSON request on stdin, one JSON response
on stdout, output capped, killed after its timeout) and only that process reads the
connector config file. This module never imports imaplib, ssl or socket.

`MailCache` keeps message metadata (never bodies) in state_dir/mail-cache.json:
atomic 0600 writes, every read-modify-write under an flock on state_dir/mail.lock,
bounded in size and validated field by field on every read. It also enforces the
login-storm limits: at most one scan of an account per minute and at most 30 worker
starts per hour (10 for calls that come from Telegram).

Every string from the worker is cleaned again here (the worker parses hostile
data; a parser bug there must not pass escape sequences to a console or a phone).

What leaves the machine over Telegram is set by PA_MAIL_TELEGRAM: `off`, `counts`
(default: unread and new per folder), `subjects` (plus sender names and subjects)
or `full` (plus `mail show`, 1500 characters, one-time codes masked). Every
third-party string on Telegram goes through notify.telegram_untrusted, so a subject
such as '/lock' cannot become a tappable bot command.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import logging
import math
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterator, Mapping

from .mailconfig import ADDRESS_RE, NAME_RE, file_problem
from .mailparse import SIGNAL_CODES, SIGNAL_TEXTS, clean_block, clean_line, display_subject, domain_of, \
    mask_codes, message_ref
from .mailtriage import PayeeStore, category_counts, classify, find_ibans, health_notes, payee_lines
from .mailtriage import label as category_label
from .notify import telegram_untrusted
from .statefiles import read_private_json, write_private_json

if TYPE_CHECKING:
    from .config import Config

log = logging.getLogger(__name__)

WORKER_PATH = Path(__file__).resolve().parent / "mail_worker.py"
PROTOCOL = 1
ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR")
MAX_REQUEST = 256 * 1024
MAX_WORKER_OUTPUT = 4 * 1024 * 1024
MAX_STDERR = 16 * 1024
WORKER_CODES = ("config", "timeout", "crashed", "output_too_large", "bad_output", "unavailable")

CACHE_FILE = "mail-cache.json"
LOCK_FILE = "mail.lock"
CACHE_VERSION = 1
CACHE_CAP = 2 * 1024 * 1024
MAX_ACCOUNTS = 5
MAX_FOLDERS = 10
MAX_PER_FOLDER = 100
MAX_RECORDS = 1000
LOCK_WAIT_S = 5.0
SCAN_GAP_S = 60.0
SPAWNS_PER_HOUR = 30
TELEGRAM_SPAWNS_PER_HOUR = 10

SHOW_CHARS_CONSOLE = 4000
SHOW_CHARS_TELEGRAM = 1500
CONSOLE_GUTTER = "│ "
TELEGRAM_GUTTER = "> "
NOT_CONFIGURED = "mail is not configured (PA_MAIL_CONFIG)"
REF_RE = re.compile(r"[0-9a-f]{6}")
_FOLDER_OK = re.compile(r"[^\x00-\x1f\x7f]{1,100}")
_LOCK_FLAGS = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC

MAIL_WORDS = ("mail",)
REFRESH_WORDS = ("refresh", "aktualisieren")
SHOW_WORDS = ("show", "zeige")
DRAFT_WORDS = ("draft", "entwurf")
TRUST_WORDS = ("trust", "vertrauen")
DRAFTS_DIR = ("outputs", "drafts")  # vault folder the assistant writes reply drafts to: <id>.md
MAX_DRAFT_FILE = 20_000


# --- the worker process ----------------------------------------------------------------


class WorkerError(RuntimeError):
    """The worker could not answer. code is one of WORKER_CODES; detail (config errors only)
    names a key path of the config file, never a value."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code)
        self.code = code if code in WORKER_CODES else "bad_output"
        self.detail = detail


class WorkerClient:
    """Starts the mail worker for one request at a time."""

    KILL_GRACE_S = 5.0  # after the request's own deadline

    def __init__(self, config_path: Path, timeout_s: int, *, run_dir: Path, python: str = sys.executable,
                 worker: Path | None = None, env_base: Mapping[str, str] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.config_path = Path(config_path)
        self.timeout_s = timeout_s
        self.run_dir = Path(run_dir)
        self.python = python
        self.worker = Path(worker) if worker is not None else WORKER_PATH
        self.env_base = env_base
        self.clock = clock

    def __repr__(self) -> str:
        return "WorkerClient(<mail worker>)"

    def argv(self) -> list[str]:
        return [self.python, "-I", "-S", str(self.worker.resolve())]

    def environment(self) -> dict[str, str]:
        """Only harmless variables plus PA_MAIL_CONFIG: never tokens, PINs or Claude settings."""
        base = os.environ if self.env_base is None else self.env_base
        env = {name: base[name] for name in ENV_KEEP if name in base}
        env["PA_MAIL_CONFIG"] = str(self.config_path)
        return env

    def call(self, command: str, payload: dict, timeout_s: float | None = None) -> dict:
        """One request -> the worker's response dict. Raises WorkerError."""
        timeout = float(timeout_s if timeout_s is not None else self.timeout_s)
        request = {**payload, "protocol": PROTOCOL, "command": command, "deadline_s": timeout}
        data = json.dumps(request, ensure_ascii=False).encode("utf-8")
        if len(data) > MAX_REQUEST:
            raise WorkerError("unavailable")
        if not os.path.isabs(self.python):
            raise WorkerError("unavailable")
        started = self.clock()
        try:
            self.run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                self.argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=self.run_dir, env=self.environment(), start_new_session=True, close_fds=True,
            )
        except OSError as exc:
            log.warning("mail worker %s: cannot start (%s)", command, type(exc).__name__)
            raise WorkerError("unavailable") from None
        out = bytearray()
        err_size = [0]
        overflow = threading.Event()

        def write() -> None:
            try:
                assert proc.stdin is not None
                proc.stdin.write(data)
                proc.stdin.close()
            except OSError:
                pass

        def read_out() -> None:
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    return
                if len(out) + len(chunk) > MAX_WORKER_OUTPUT:
                    overflow.set()
                    _kill_group(proc)
                    return
                out.extend(chunk)

        def read_err() -> None:
            # Discarded (it could contain server text); only its size is counted.
            assert proc.stderr is not None
            while True:
                chunk = proc.stderr.read(4096)
                if not chunk:
                    return
                err_size[0] = min(MAX_STDERR + 1, err_size[0] + len(chunk))

        threads = [threading.Thread(target=fn, daemon=True, name=f"mail-worker-{fn.__name__}")
                   for fn in (write, read_out, read_err)]
        for thread in threads:
            thread.start()
        timed_out = False
        try:
            proc.wait(timeout=timeout + self.KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc)
        for thread in threads:
            thread.join(5.0)
        for stream in (proc.stdout, proc.stderr):
            with contextlib.suppress(OSError, ValueError):
                stream.close()  # type: ignore[union-attr]
        duration = self.clock() - started
        if err_size[0]:
            log.debug("mail worker %s: stderr discarded (%d bytes)", command, err_size[0])
        if timed_out:
            log.warning("mail worker %s: timeout after %.1fs", command, duration)
            raise WorkerError("timeout")
        if overflow.is_set():
            log.warning("mail worker %s: output too large", command)
            raise WorkerError("output_too_large")
        if not out:
            log.warning("mail worker %s: no output (exit %s)", command, proc.returncode)
            raise WorkerError("crashed" if proc.returncode else "bad_output")
        try:
            response = json.loads(bytes(out).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise WorkerError("bad_output") from None
        if not isinstance(response, dict) or response.get("protocol") != PROTOCOL \
                or not isinstance(response.get("ok"), bool):
            raise WorkerError("bad_output")
        errors = response.get("errors")
        if not isinstance(errors, list):
            raise WorkerError("bad_output")
        codes = sorted({str(e.get("code")) for e in errors if isinstance(e, dict)})
        log.info("mail worker %s: %.1fs%s", command, duration, f" ({', '.join(codes)})" if codes else "")
        for error in errors:
            if isinstance(error, dict) and error.get("code") == "config" and "account" not in error:
                detail = error.get("detail")
                raise WorkerError("config", clean_line(detail, 160) if isinstance(detail, str) else None)
        return response


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGTERM to the worker's process group, SIGKILL 2 s later; returns once it is reaped."""
    for sig, wait_s in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=wait_s)
            return
        except subprocess.TimeoutExpired:
            continue


# --- the cache -------------------------------------------------------------------------


@dataclass(frozen=True)
class MessageMeta:
    ref: str
    account: str
    folder: str
    uidvalidity: int
    uid: int
    date: float | None
    from_name: str
    from_addr: str | None
    subject: str
    seen: bool
    answered: bool
    is_list: bool
    is_auto: bool
    size: int

    @property
    def key(self) -> tuple[str, str, int, int]:
        return self.account, self.folder, self.uidvalidity, self.uid


@dataclass(frozen=True)
class FolderView:
    name: str
    uidvalidity: int | None
    uidnext: int | None
    messages: int | None
    unseen: int | None
    checked_at: float | None
    error: str | None
    window: tuple[MessageMeta, ...]


@dataclass(frozen=True)
class AccountView:
    name: str
    error: str | None
    checked_at: float | None
    folders: tuple[FolderView, ...]


@dataclass(frozen=True)
class CacheView:
    checked_at: float | None
    accounts: tuple[AccountView, ...]

    def messages(self) -> list[MessageMeta]:
        return [m for a in self.accounts for f in a.folders for m in f.window]

    def find(self, ref: str) -> MessageMeta | None:
        for message in self.messages():
            if message.ref == ref:
                return message
        return None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _count(value: object, high: int = 2 ** 32) -> int | None:
    if type(value) is not int or not 0 <= value < high:
        return None
    return value


def _code(value: object) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[a-z_]{1,24}", value) else None


def _address(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 254 or not ADDRESS_RE.fullmatch(value):
        return None
    return value.lower()


def clean_record(raw: object) -> dict | None:
    """One message record (from the cache file or the worker) re-validated and re-cleaned."""
    if not isinstance(raw, dict):
        return None
    uid = _count(raw.get("uid"))
    if not uid:
        return None
    flags = raw.get("flags")
    seen = flags.get("seen") if isinstance(flags, dict) else raw.get("seen")
    answered = flags.get("answered") if isinstance(flags, dict) else raw.get("answered")
    is_list = raw.get("list")
    is_auto = raw.get("auto")
    return {
        "uid": uid,
        "date": _number(raw.get("date")),
        "from_name": clean_line(raw.get("from_name") if isinstance(raw.get("from_name"), str) else "", 60),
        "from_addr": _address(raw.get("from_addr")),
        "subject": clean_line(raw.get("subject") if isinstance(raw.get("subject"), str) else "", 200),
        "seen": seen is True,
        "answered": answered is True,
        "list": is_list is True,
        "auto": is_auto is True,
        "size": _count(raw.get("size")) or 0,
    }


def _folder_name(value: object) -> str | None:
    return value if isinstance(value, str) and _FOLDER_OK.fullmatch(value) and value == value.strip() else None


def _empty() -> dict:
    return {"version": CACHE_VERSION, "checked_at": None, "accounts": {}, "limits": {"spawns": [], "scans": {}}}


def validate_cache(data: object) -> dict:
    """The cache file normalised: anything invalid is dropped (an invalid file is empty)."""
    result = _empty()
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return result
    result["checked_at"] = _number(data.get("checked_at"))
    accounts = data.get("accounts")
    for name, account in list(accounts.items())[:MAX_ACCOUNTS] if isinstance(accounts, dict) else ():
        if not isinstance(name, str) or not NAME_RE.fullmatch(name) or not isinstance(account, dict):
            continue
        folders_out: dict = {}
        folders = account.get("folders")
        for folder, info in list(folders.items())[:MAX_FOLDERS] if isinstance(folders, dict) else ():
            if _folder_name(folder) is None or not isinstance(info, dict):
                continue
            window: list[dict] = []
            raw_window = info.get("messages_window")
            for raw in raw_window[:MAX_PER_FOLDER] if isinstance(raw_window, list) else ():
                record = clean_record(raw)
                ref = raw.get("ref") if isinstance(raw, dict) else None
                if record is None or not isinstance(ref, str) or not REF_RE.fullmatch(ref):
                    continue
                window.append({"ref": ref, **record})
            folders_out[folder] = {
                "uidvalidity": _count(info.get("uidvalidity")), "uidnext": _count(info.get("uidnext")),
                "messages": _count(info.get("messages")), "unseen": _count(info.get("unseen")),
                "checked_at": _number(info.get("checked_at")), "error": _code(info.get("error")),
                "messages_window": window,
            }
        result["accounts"][name] = {"error": _code(account.get("error")),
                                    "checked_at": _number(account.get("checked_at")), "folders": folders_out}
    limits = data.get("limits")
    if isinstance(limits, dict):
        spawns = limits.get("spawns")
        for entry in spawns[-200:] if isinstance(spawns, list) else ():
            if isinstance(entry, list) and len(entry) == 2 and _number(entry[0]) is not None \
                    and entry[1] in ("console", "telegram"):
                result["limits"]["spawns"].append([float(entry[0]), entry[1]])
        scans = limits.get("scans")
        for name, at in scans.items() if isinstance(scans, dict) else ():
            if isinstance(name, str) and NAME_RE.fullmatch(name) and _number(at) is not None:
                result["limits"]["scans"][name] = float(at)
    return result


def _view(data: dict) -> CacheView:
    accounts: list[AccountView] = []
    for name, account in data["accounts"].items():
        folders: list[FolderView] = []
        for folder, info in account["folders"].items():
            window = tuple(
                MessageMeta(ref=r["ref"], account=name, folder=folder, uidvalidity=info["uidvalidity"] or 0,
                            uid=r["uid"], date=r["date"], from_name=r["from_name"], from_addr=r["from_addr"],
                            subject=r["subject"], seen=r["seen"], answered=r["answered"], is_list=r["list"],
                            is_auto=r["auto"], size=r["size"])
                for r in info["messages_window"])
            folders.append(FolderView(folder, info["uidvalidity"], info["uidnext"], info["messages"],
                                      info["unseen"], info["checked_at"], info["error"], window))
        accounts.append(AccountView(name, account["error"], account["checked_at"], tuple(folders)))
    return CacheView(data["checked_at"], tuple(accounts))


class MailCache:
    """mail-cache.json in the state directory, changed only under mail.lock."""

    def __init__(self, state_dir: Path, clock: Callable[[], float] = time.time) -> None:
        self.state_dir = Path(state_dir)
        self.clock = clock
        self._corrupt_logged = False

    def _read(self) -> dict:
        raw = read_private_json(self.state_dir, CACHE_FILE, CACHE_CAP, CACHE_VERSION)
        if raw is None:
            if os.path.lexists(self.state_dir / CACHE_FILE) and not self._corrupt_logged:
                self._corrupt_logged = True
                log.warning("mail cache unreadable: treated as empty and rewritten on the next refresh")
            return _empty()
        return validate_cache(raw)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.state_dir / LOCK_FILE, _LOCK_FLAGS, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(errno.EINVAL, "mail lock is not a regular file")
            end = time.monotonic() + LOCK_WAIT_S
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                        raise
                    if time.monotonic() >= end:
                        raise TimeoutError("mail cache busy") from None
                    time.sleep(0.05)
            yield
        finally:
            os.close(fd)  # releases the flock

    def _save(self, data: dict) -> bool:
        return write_private_json(self.state_dir, CACHE_FILE, data, prefix=".mail-")

    def load(self) -> CacheView:
        return _view(self._read())

    def find(self, ref: str) -> MessageMeta | None:
        return self.load().find(ref) if REF_RE.fullmatch(ref or "") else None

    def age_s(self) -> float | None:
        checked = self._read()["checked_at"]
        return None if checked is None else max(0.0, self.clock() - checked)

    def reserve(self, origin: str, scan_accounts: list[str] | None = None) -> tuple[bool, list[str]]:
        """Count one worker start (and, for a scan, one scan per account) against the limits.

        Returns (allowed, accounts that may be scanned now). Not allowed: more than
        SPAWNS_PER_HOUR starts in the last hour (TELEGRAM_SPAWNS_PER_HOUR from Telegram),
        or every named account was scanned less than SCAN_GAP_S ago."""
        origin = "telegram" if origin == "telegram" else "console"
        now = self.clock()
        with self._locked():
            data = self._read()
            limits = data["limits"]
            spawns = [s for s in limits["spawns"] if 0 <= now - s[0] < 3600]
            if len(spawns) >= SPAWNS_PER_HOUR or (
                    origin == "telegram" and sum(1 for s in spawns if s[1] == "telegram") >= TELEGRAM_SPAWNS_PER_HOUR):
                return False, []
            allowed: list[str] = []
            if scan_accounts is not None:
                scans = limits["scans"]
                allowed = [a for a in scan_accounts if not 0 <= now - scans.get(a, -1e18) < SCAN_GAP_S]
                if scan_accounts and not allowed:
                    return False, []
                for name in allowed:
                    scans[name] = now
            spawns.append([now, origin])
            limits["spawns"] = spawns
            self._save(data)
        return True, allowed

    def merge_scan(self, response: dict) -> CacheView:
        """Merge a worker `scan` response (validated and cleaned again) into the cache."""
        now = self.clock()
        with self._locked():
            data = self._read()
            refs = {(name, folder, info["uidvalidity"], r["uid"]): r["ref"]
                    for name, account in data["accounts"].items()
                    for folder, info in account["folders"].items() for r in info["messages_window"]}
            for result in (response.get("accounts") or [])[:MAX_ACCOUNTS]:
                if not isinstance(result, dict):
                    continue
                name = result.get("name")
                if not isinstance(name, str) or not NAME_RE.fullmatch(name):
                    continue
                account = data["accounts"].setdefault(name, {"error": None, "checked_at": None, "folders": {}})
                account["error"] = _code(result.get("error"))
                account["checked_at"] = now
                data["limits"]["scans"][name] = now
                if account["error"]:
                    continue  # keep what was cached
                folders = result.get("folders")
                seen_folders: set[str] = set()
                for folder, info in list(folders.items())[:MAX_FOLDERS] if isinstance(folders, dict) else ():
                    if _folder_name(folder) is None or not isinstance(info, dict):
                        continue
                    seen_folders.add(folder)
                    old = account["folders"].get(folder)
                    error = _code(info.get("error"))
                    uidvalidity = _count(info.get("uidvalidity"))
                    if error or not uidvalidity:
                        if old is not None:
                            old["error"] = error or "protocol"
                            old["checked_at"] = now
                        else:
                            account["folders"][folder] = {
                                "uidvalidity": None, "uidnext": None, "messages": None, "unseen": None,
                                "checked_at": now, "error": error or "protocol", "messages_window": []}
                        continue
                    if old is not None and old.get("uidvalidity") != uidvalidity:
                        log.info("mail %s: folder ids changed (UIDVALIDITY); its records were dropped", name)
                    window: list[dict] = []
                    raw_records = info.get("records")
                    records = [r for r in (clean_record(x) for x in (raw_records or [])[:MAX_PER_FOLDER]
                                           if isinstance(raw_records, list)) if r]
                    for record in sorted(records, key=lambda r: r["uid"], reverse=True):
                        key = (name, folder, uidvalidity, record["uid"])
                        ref = refs.get(key)
                        if ref is None:
                            ref = message_ref(name, folder, uidvalidity, record["uid"], set(refs.values()))
                            refs[key] = ref
                        window.append({"ref": ref, **record})
                    account["folders"][folder] = {
                        "uidvalidity": uidvalidity, "uidnext": _count(info.get("uidnext")),
                        "messages": _count(info.get("messages")), "unseen": _count(info.get("unseen")),
                        "checked_at": now, "error": None, "messages_window": window}
                for folder in list(account["folders"]):
                    if folder not in seen_folders and isinstance(folders, dict) and folders:
                        del account["folders"][folder]  # no longer configured
            _bound(data)
            data["checked_at"] = now
            self._save(data)
            return _view(data)


def _bound(data: dict) -> None:
    """At most MAX_RECORDS records overall: the oldest go first."""
    entries = [(r.get("date") or 0.0, name, folder, r["uid"])
               for name, account in data["accounts"].items()
               for folder, info in account["folders"].items() for r in info["messages_window"]]
    if len(entries) <= MAX_RECORDS:
        return
    drop = {(n, f, u) for _, n, f, u in sorted(entries)[: len(entries) - MAX_RECORDS]}
    for name, account in data["accounts"].items():
        for folder, info in account["folders"].items():
            info["messages_window"] = [r for r in info["messages_window"] if (name, folder, r["uid"]) not in drop]


# --- commands --------------------------------------------------------------------------


@dataclass(frozen=True)
class MailCommand:
    kind: str  # mail_overview, mail_refresh, mail_show, mail_draft, mail_trust, mail_usage
    arg: str = ""


def parse_mail_command(text: str) -> MailCommand | None:
    """The tier-1 mail command in text, or None. "!mail ..." always goes to the assistant."""
    s = text.strip()
    if not s or s.startswith("!"):
        return None
    tokens = s.split()
    if tokens[0].lower() not in MAIL_WORDS:
        return None
    if len(tokens) == 1:
        return MailCommand("mail_overview")
    second = tokens[1].lower()
    if len(tokens) == 2 and second in REFRESH_WORDS:
        return MailCommand("mail_refresh")
    if len(tokens) == 3 and REF_RE.fullmatch(tokens[2].lower()):
        for words, kind in ((SHOW_WORDS, "mail_show"), (DRAFT_WORDS, "mail_draft"), (TRUST_WORDS, "mail_trust")):
            if second in words:
                return MailCommand(kind, tokens[2].lower())
    return MailCommand("mail_usage")


def _hhmm(ts: float | None) -> str:
    if ts is None:
        return "--:--"
    try:
        return datetime.fromtimestamp(ts).strftime("%H:%M")
    except (OverflowError, OSError, ValueError):
        return "--:--"


def _when(ts: float | None, now: float) -> str:
    if ts is None:
        return "     "
    try:
        moment, today = datetime.fromtimestamp(ts), datetime.fromtimestamp(now)
    except (OverflowError, OSError, ValueError):
        return "     "
    return moment.strftime("%H:%M") if moment.date() == today.date() else moment.strftime("%m-%d")


def _pad(text: str, width: int) -> str:
    text = clean_line(text, width)
    return text + " " * max(0, width - len(text))


class MailService:
    """The tier-1 mail commands for one front end (the chat or serve's Telegram assistant)."""

    def __init__(self, config: "Config", worker: WorkerClient | None, cache: MailCache, *,
                 problem: str | None = None, clock: Callable[[], float] = time.time,
                 since: Callable[[], float | None] | None = None) -> None:
        self.config = config
        self.worker = worker
        self.cache = cache
        self.problem = problem
        self.clock = clock
        self._since = since

    def __repr__(self) -> str:
        return "MailService()"

    @classmethod
    def from_config(cls, config: "Config") -> "MailService | None":
        """None when PA_MAIL_CONFIG is unset. The file is only lstat'ed, never read."""
        if config.mail_config is None:
            return None
        problem = file_problem(config.mail_config)
        worker = WorkerClient(config.mail_config, config.mail_timeout_s, run_dir=config.state_dir / "run")
        return cls(config, worker, MailCache(config.state_dir), problem=problem)

    # --- helpers ---

    def forbidden_roots(self) -> list[str]:
        roots = [self.config.vault_path, self.config.state_dir]
        return [str(Path(p).expanduser().resolve()) for p in roots]

    def since(self) -> tuple[float, bool]:
        """(start of "new", whether it is the last brief): the last brief, else 24 hours ago."""
        last: float | None = None
        if self._since is not None:
            last = self._since()
        else:
            try:
                from .brief import load_state

                state, _ = load_state(self.config)
                value = state.get("last_brief_at") if state else None
                last = _number(value)
            except Exception as exc:  # noqa: BLE001 - "new" falls back to 24 hours
                log.warning("mail: brief state unreadable (%s)", type(exc).__name__)
        if last is not None:
            return last, True
        return self.clock() - 24 * 3600, False

    def telegram_mode(self) -> str:
        return self.config.mail_telegram

    def available(self, kind: str, channel: str) -> bool:
        if channel != "telegram":
            return True
        mode = self.telegram_mode()
        if mode == "off":
            return False
        if kind in ("mail_show", "mail_draft", "mail_trust"):
            return mode == "full"
        return True

    def help_line(self, channel: str) -> str | None:
        if channel == "telegram":
            mode = self.telegram_mode()
            if mode == "off":
                return None
            show = " · mail show <id>" if mode == "full" else ""
            return f"Mail (read-only, PA_MAIL_TELEGRAM={mode}): mail · mail refresh{show}."
        return ("Mail (nothing is sent, nothing is marked as read): mail lists counts, categories and the latest "
                "mails; mail refresh checks now; mail show <id> shows one mail as plain text; mail draft <id> puts "
                "the reply in outputs/drafts/<id>.md into the account's drafts folder; mail trust <id> confirms "
                "the bank account in a mail after you checked it.")

    # --- commands ---

    def command(self, cmd: MailCommand, channel: str = "console") -> tuple[str, bool]:
        if not self.available(cmd.kind, channel):
            if self.telegram_mode() == "off":
                return "Mail is not available over Telegram (PA_MAIL_TELEGRAM=off).", False
            return "Not available over Telegram: mail show, draft and trust need PA_MAIL_TELEGRAM=full.", False
        if cmd.kind == "mail_usage":
            usage = "usage: mail · mail refresh"
            if self.available("mail_show", channel):
                usage += " · mail show <id> · mail draft <id> · mail trust <id>"
            return usage, False
        if cmd.kind == "mail_show":
            return self._show(cmd.arg, channel)
        if cmd.kind == "mail_draft":
            return self._draft(cmd.arg)
        if cmd.kind == "mail_trust":
            return self._trust(cmd.arg, channel)
        return self._overview(channel, force=cmd.kind == "mail_refresh")

    def refresh(self, force: bool, origin: str) -> tuple[CacheView, str]:
        """The cached view, refreshed through the worker when older than PA_MAIL_CACHE_S (or
        forced and allowed by the limits). The note explains a stale view."""
        view = self.cache.load()
        now = self.clock()
        fresh = view.checked_at is not None and 0 <= now - view.checked_at < self.config.mail_cache_s
        if fresh and not force:
            return view, ""
        as_of = f"as of {_hhmm(view.checked_at)}"
        if self.problem or self.worker is None:
            return view, f"({as_of}; {self.problem or 'mail worker unavailable'})"
        known = [a.name for a in view.accounts]
        try:
            allowed, accounts = self.cache.reserve(origin, known)
        except (OSError, TimeoutError) as exc:
            return view, f"({as_of}; refresh failed: cache {type(exc).__name__})"
        if not allowed:
            return view, f"(refresh limited, {as_of})"
        payload: dict = {"days": self.config.mail_days, "max_per_folder": self.config.mail_max_per_folder,
                         "forbidden_roots": self.forbidden_roots()}
        if known and set(accounts) != set(known):
            payload["accounts"] = accounts
        try:
            response = self.worker.call("scan", payload)
        except WorkerError as exc:
            detail = f": {exc.detail}" if exc.detail and origin != "telegram" else ""
            return view, f"({as_of}; refresh failed: {exc.code}{detail})"
        try:
            view = self.cache.merge_scan(response)
        except (OSError, TimeoutError) as exc:
            return view, f"({as_of}; refresh failed: cache {type(exc).__name__})"
        failed = [f"{e.get('account')}: {e.get('code')}" for e in response.get("errors", [])
                  if isinstance(e, dict) and isinstance(e.get("account"), str) and NAME_RE.fullmatch(e["account"])
                  and _code(e.get("code"))]
        return view, f"(refresh failed: {', '.join(failed)})" if failed else ""

    def _overview(self, channel: str, force: bool) -> tuple[str, bool]:
        view, note = self.refresh(force, channel)
        if view.checked_at is None and not view.accounts:
            return f"Mail: nothing to show yet {note}".rstrip(), bool(note)
        since, from_brief = self.since()
        now = self.clock()
        telegram = channel == "telegram"
        mode = self.telegram_mode() if telegram else "full"
        head = f"Mail (as of {_hhmm(view.checked_at)}" + ("" if telegram else f" · last {self.config.mail_days} days")
        lines = [head + ")" + (f" {note}" if note else "")]
        new_label = f"new since the brief ({_hhmm(since)})" if from_brief else "new in the last 24 h"
        messages: list[MessageMeta] = []
        labelled = False
        for account in view.accounts:
            name = clean_line(account.name, 24)
            if account.error:
                lines.append(f"{name}: unavailable ({account.error})")
            for folder in account.folders:
                fname = clean_line(folder.name, 40)
                if telegram:
                    fname = telegram_untrusted(fname, 40)
                if folder.error:
                    lines.append(f"{name} · {fname}: {folder.error}")
                    continue
                new = sum(1 for m in folder.window if m.date is not None and m.date > since)
                unread = folder.unseen if folder.unseen is not None else sum(1 for m in folder.window if not m.seen)
                label = "new" if telegram or labelled else new_label
                labelled = True
                line = f"{name} · {fname}: {unread} unread · {new} {label}"
                if not telegram:
                    line += f" · {len(folder.window)} in window"
                lines.append(line)
                messages.extend(folder.window)
        language = self.config.language
        for note in health_notes(view, now, language):
            lines.append(("Check: " if language != "de" else "Prüfen: ") + self._inert(note, 160, telegram))
        counts = category_counts((m.subject, m.is_list, m.is_auto) for m in messages)
        if counts:
            lines.append(" · ".join(f"{category_label(c, language)} {n}" for c, n in counts))
        if telegram and mode == "counts":
            return "\n".join(lines), False
        names = self._sender_names(messages)
        counter: Counter[str] = Counter()
        label_of: dict[str, str] = {}
        for message in messages:
            key = message.from_addr or f"name:{message.from_name}"
            counter[key] += 1
            label_of.setdefault(key, names(message))
        if counter:
            top = ", ".join(f"{self._inert(label_of[key], 24, telegram)} ({count})"
                            for key, count in counter.most_common(5))
            lines.append(f"Top senders: {top}")
        latest = sorted(messages, key=lambda m: (m.seen, -(m.date or 0.0)))[:10]
        if latest:
            lines.append("Latest:")
            for message in latest:
                category = classify(message.subject, is_list=message.is_list, is_auto=message.is_auto)
                labels = [category_label(category, language)] if category not in ("other", "automated") else []
                labels += [name for name, on in (("unread", not message.seen), ("answered", message.answered),
                                                 ("list", message.is_list), ("auto", message.is_auto)) if on]
                if telegram:
                    sender = telegram_untrusted(names(message), 24)
                    subject = telegram_untrusted(display_subject(message.subject, 60), 60)
                    lines.append(f"  {message.ref} {_when(message.date, now)} {sender} · {subject}"
                                 + (f" · {' · '.join(labels)}" if labels else ""))
                else:
                    lines.append(f"  {message.ref} {_when(message.date, now)} {_pad(names(message), 20)} "
                                 f"{_pad(display_subject(message.subject, 40), 40)}  {' · '.join(labels)}".rstrip())
        lines.append("mail show <id> · mail refresh" if self.available("mail_show", channel) else "mail refresh")
        return "\n".join(lines), False

    @staticmethod
    def _inert(text: str, limit: int, telegram: bool) -> str:
        return telegram_untrusted(text, limit) if telegram else clean_line(text, limit)

    @staticmethod
    def _sender_names(messages: list[MessageMeta]) -> Callable[[MessageMeta], str]:
        """A display name; when one name is used by several sender domains in the window, the
        domain is shown next to it ('Alice Example (evil.example)')."""
        domains: dict[str, set[str]] = {}
        for message in messages:
            if message.from_name:
                domains.setdefault(message.from_name.casefold(), set()).add(domain_of(message.from_addr) or "?")

        def name(message: MessageMeta) -> str:
            base = message.from_name or message.from_addr or "(unknown sender)"
            if message.from_name and len(domains.get(message.from_name.casefold(), ())) > 1:
                return f"{base} ({domain_of(message.from_addr) or 'no address'})"
            return base

        return name

    def _show(self, ref: str, channel: str) -> tuple[str, bool]:
        telegram = channel == "telegram"
        chars = SHOW_CHARS_TELEGRAM if telegram else SHOW_CHARS_CONSOLE
        meta, item, error = self._fetch(ref, channel, chars)
        if error:
            return error, True
        assert meta is not None and item is not None
        return self._render_show(meta, item, chars, telegram), False

    def _fetch(self, ref: str, channel: str, chars: int) -> tuple[MessageMeta | None, dict | None, str | None]:
        """(meta, body item, error text) for one cached mail, through the worker."""
        meta = self.cache.find(ref)
        if meta is None:
            return None, None, "unknown mail id; run mail"
        if self.problem or self.worker is None:
            return meta, None, f"mail could not be read: {self.problem or 'mail worker unavailable'}"
        try:
            allowed, _ = self.cache.reserve(channel)
        except (OSError, TimeoutError) as exc:
            return meta, None, f"mail could not be read (cache {type(exc).__name__})"
        if not allowed:
            return meta, None, "mail could not be read now (too many mail requests this hour; try later)"
        payload = {"items": [{"account": meta.account, "folder": meta.folder, "uidvalidity": meta.uidvalidity,
                              "uid": meta.uid}], "body_chars": chars, "forbidden_roots": self.forbidden_roots()}
        try:
            response = self.worker.call("bodies", payload)
        except WorkerError as exc:
            return meta, None, f"mail could not be read ({exc.code})"
        items = response.get("items")
        item = items[0] if isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict) else None
        if item is None or item.get("ref_key") != list(meta.key):
            return meta, None, "mail could not be read (protocol)"
        if "error" in item:
            code = _code(item.get("error")) or "protocol"
            if code == "mismatch":
                return meta, None, "this mail id is stale (the folder changed); run mail refresh"
            return meta, None, f"mail could not be read ({code})"
        return meta, item, None

    def _trust(self, ref: str, channel: str) -> tuple[str, bool]:
        """Confirm the IBANs in one mail for its sender (the person checked them by phone)."""
        meta, item, error = self._fetch(ref, channel, SHOW_CHARS_CONSOLE)
        if error:
            return error, True
        assert meta is not None and item is not None
        sender = _address(item.get("from_addr")) or meta.from_addr
        text = item.get("text") if isinstance(item.get("text"), str) else ""
        ibans = find_ibans(text)
        if not sender or not ibans:
            return "no bank account (IBAN) with a sender address found in this mail", True
        added = PayeeStore(self.config.state_dir).trust(sender, ibans)
        return f"{added} bank account(s) confirmed for this sender; future mails with them are not flagged", False

    def _draft_file(self, ref: str) -> tuple[Path | None, str | None]:
        root = Path(self.config.vault_path).expanduser()
        path = root.joinpath(*DRAFTS_DIR, f"{ref}.md")
        try:
            info = os.lstat(path)
        except OSError:
            return None, f"no draft yet: write the reply to {'/'.join(DRAFTS_DIR)}/{ref}.md in the vault first"
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_DRAFT_FILE * 4:
            return None, "the draft file must be a regular file of at most 20000 characters"
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root.resolve())
        except (OSError, ValueError):
            return None, "the draft file must be inside the vault"
        return resolved, None

    def _draft(self, ref: str) -> tuple[str, bool]:
        """Put the vault file outputs/drafts/<id>.md into the drafts folder as a reply. Never sends."""
        meta = self.cache.find(ref)
        if meta is None:
            return "unknown mail id; run mail", True
        if not meta.from_addr:
            return "this mail has no valid sender address to reply to", True
        if self.problem or self.worker is None:
            return f"draft not saved: {self.problem or 'mail worker unavailable'}", True
        path, error = self._draft_file(ref)
        if error:
            return error, True
        try:
            body = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return f"draft not saved: the file could not be read ({type(exc).__name__})", True
        if len(body) > MAX_DRAFT_FILE:
            return "draft not saved: the file is longer than 20000 characters", True
        subject = meta.subject if meta.subject.lower().startswith(("re:", "aw:")) else f"Re: {meta.subject}"
        try:
            response = self.worker.call("draft", {"account": meta.account, "to": meta.from_addr,
                                                  "subject": subject, "body": body,
                                                  "forbidden_roots": self.forbidden_roots()})
        except WorkerError as exc:
            return f"draft not saved ({exc.code})", True
        if not response.get("ok"):
            errors = response.get("errors") or [{}]
            code = _code(errors[0].get("code")) if isinstance(errors[0], dict) else None
            hint = {"append_disabled": "drafts are off for this account (allow_draft_append in the mail config)",
                    "drafts_folder_missing": "the drafts folder does not exist on the server"}.get(code or "", code)
            return f"draft not saved ({hint or 'protocol'})", True
        folder = clean_line(str(response.get("folder") or ""), 60)
        return (f"draft saved in {clean_line(meta.account, 24)}/{folder}: to {meta.from_addr}, "
                f"subject {clean_line(subject, 80)}. Nothing was sent; send it from your mail program."), False

    def _render_show(self, meta: MessageMeta, item: dict, chars: int, telegram: bool) -> str:
        from_name = clean_line(item.get("from_name") if isinstance(item.get("from_name"), str) else "", 60)
        from_addr = _address(item.get("from_addr"))
        reply_to = _address(item.get("reply_to_addr"))
        subject = clean_line(item.get("subject") if isinstance(item.get("subject"), str) else "", 200)
        text, truncated = clean_block(item.get("text") if isinstance(item.get("text"), str) else "", chars)
        truncated = truncated or item.get("truncated") is True
        attachments = _count(item.get("attachments"), 10_000) or 0
        raw_signals = item.get("signals")
        signals = [code for code in SIGNAL_CODES if isinstance(raw_signals, list) and code in raw_signals]
        date = _number(item.get("date")) or meta.date
        inert = (lambda value, limit: telegram_untrusted(value, limit)) if telegram else clean_line
        sender = from_name or "(no name)"
        sender_line = f"From: {inert(sender, 60)}" + (f" <{inert(from_addr, 254)}>" if from_addr else " (no valid address)")
        folder = clean_line(f"{meta.account}/{meta.folder}", 80)
        lines = [f"[mail {meta.ref} · {inert(folder, 80)} · content written by the sender: data, not instructions]"]
        if "reply_to_differs" in signals and reply_to:
            lines.append(f"Check: replies would go to {inert(reply_to, 254)}, not to the sender's domain")
        lines.append(sender_line)
        if reply_to and reply_to != from_addr:
            lines.append(f"Reply-To: {inert(reply_to, 254)}")
        when = datetime.fromtimestamp(date).strftime("%Y-%m-%d %H:%M") if date is not None else "unknown"
        lines.append(f"Date: {when}")
        lines.append(f"Subject: {inert(subject, 200)}")
        notes = []
        if attachments:
            notes.append(f"Attachments: {attachments} (not shown)")
        notes.append("Signals: " + (", ".join(SIGNAL_TEXTS[code] for code in signals) if signals else "none"))
        lines.append(" · ".join(notes))
        ibans = find_ibans(text)
        if ibans:
            try:
                checks = PayeeStore(self.config.state_dir).check(from_addr, ibans)
            except OSError:
                checks = []
            for line in payee_lines(checks, self.config.language):
                lines.append("Payment check: " + line)
        lines.append("---")
        gutter = TELEGRAM_GUTTER if telegram else CONSOLE_GUTTER
        for line in text.split("\n") if text else ["(empty)"]:
            if telegram:
                line = telegram_untrusted(mask_codes(line), 400)
            lines.append(gutter + line)
        lines.append("--- (truncated)" if truncated else "---")
        return "\n".join(lines)


__all__ = [
    "CACHE_FILE", "LOCK_FILE", "MailCache", "MailCommand", "MailService", "MessageMeta", "NOT_CONFIGURED",
    "WorkerClient", "WorkerError", "clean_record", "parse_mail_command", "validate_cache",
]
