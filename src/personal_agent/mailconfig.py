"""The connector config file (PA_MAIL_CONFIG): mail accounts and calendar feeds.

The file holds secrets (mail passwords or app passwords, secret calendar URLs).
Only the mail worker (mail_worker.py, a separate process) calls
`load_connector_config`; the chat and serve processes never read the file. They
only call `file_problem`, which looks at the directory entry (lstat) and never
opens the file, so no password or URL ever sits in their memory or environment.

Format (version 1), a JSON object, mode 0600, owned by the user:

    {"version": 1,
     "accounts": [{"name": "work", "host": "imap.example.org", "port": 993,
                   "user": "me@example.org", "password": "<app password>",
                   "folders": ["INBOX"], "address": "me@example.org",
                   "drafts_folder": "Drafts", "allow_draft_append": false,
                   "ca_file": null}],
     "calendars": [{"name": "personal", "url": "https://.../basic.ics", "ca_file": null}]}

Unknown and duplicate keys are errors (a typo such as "pasword" must not be
silently ignored). Error messages name the key path only, never a value.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import stat
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

MAX_CONFIG_BYTES = 64 * 1024
MAX_ACCOUNTS = 5
MAX_FOLDERS = 10
MAX_CALENDARS = 5
CONFIG_VERSION = 1

NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,23}")
_HOST_RE = re.compile(r"[A-Za-z0-9.-]{1,253}")
# A conservative, ASCII-only mailbox: no quoted local parts, no comments, a dotted domain.
ADDRESS_RE = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_PRINTABLE_ASCII = re.compile(r"[\x20-\x7e]+")
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
FILE_PROBLEM = "the mail config file must be a regular 0600 file owned by you"

_TOP_KEYS = frozenset(("version", "accounts", "calendars"))
_ACCOUNT_KEYS = frozenset(("name", "host", "port", "user", "password", "folders", "address", "drafts_folder",
                           "allow_draft_append", "ca_file"))
_CALENDAR_KEYS = frozenset(("name", "url", "ca_file"))


class MailConfigError(ValueError):
    """The connector config is unusable. The message names a key path, never a value."""


@dataclass(frozen=True)
class MailAccount:
    name: str
    host: str
    port: int
    user: str = field(repr=False)
    password: str = field(repr=False)
    folders: tuple[str, ...] = ("INBOX",)
    address: str | None = field(default=None, repr=False)
    drafts_folder: str | None = None
    allow_draft_append: bool = False
    ca_file: str | None = None


@dataclass(frozen=True)
class CalendarSource:
    name: str
    url: str = field(repr=False)
    ca_file: str | None = None


@dataclass(frozen=True)
class ConnectorConfig:
    accounts: tuple[MailAccount, ...]
    calendars: tuple[CalendarSource, ...] = ()

    def account(self, name: str) -> MailAccount | None:
        for account in self.accounts:
            if account.name == name:
                return account
        return None


# --- parent side: the directory entry only ------------------------------------------


def file_problem(path: Path | None) -> str | None:
    """Why the config file cannot be used, from lstat alone (the file is never opened)."""
    if path is None:
        return "mail is not configured (PA_MAIL_CONFIG)"
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "the mail config file does not exist"
    except OSError as exc:
        return f"the mail config file cannot be checked ({type(exc).__name__})"
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return FILE_PROBLEM
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        return FILE_PROBLEM
    if info.st_mode & 0o077:
        return FILE_PROBLEM
    if info.st_size > MAX_CONFIG_BYTES:
        return "the mail config file is too large"
    return None


# --- IMAP names -----------------------------------------------------------------------


def imap_utf7(name: str) -> str:
    """A mailbox name in IMAP's modified UTF-7 (RFC 3501 5.1.3)."""
    out: list[str] = []
    pending: list[str] = []

    def flush() -> None:
        if pending:
            raw = "".join(pending).encode("utf-16-be")
            out.append("&" + base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",") + "-")
            pending.clear()

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            pending.append(ch)
    flush()
    return "".join(out)


def quote_astring(value: str) -> str:
    """An IMAP quoted string: backslash and double quote escaped. ASCII only."""
    if not _PRINTABLE_ASCII.fullmatch(value or " "):
        raise MailConfigError("an IMAP argument must be printable ASCII")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def quote_mailbox(name: str) -> str:
    """A folder name ready for STATUS/EXAMINE: modified UTF-7, then quoted."""
    return quote_astring(imap_utf7(name))


# --- validation (worker side) -----------------------------------------------------------


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise MailConfigError("the mail config file has a duplicate key")
        result[key] = value
    return result


def _keys(obj: object, allowed: frozenset[str], where: str, required: tuple[str, ...]) -> dict:
    if not isinstance(obj, dict):
        raise MailConfigError(f"{where} must be an object")
    for key in obj:
        if key not in allowed:
            # The key itself is named only when it is a plain identifier (it is never a value).
            shown = key if isinstance(key, str) and re.fullmatch(r"[a-z_]{1,32}", key) else "?"
            raise MailConfigError(f"{where} has an unknown key ({shown})")
    for key in required:
        if key not in obj:
            raise MailConfigError(f"{where}.{key} is missing")
    return obj


def _name(value: object, where: str) -> str:
    if not isinstance(value, str) or not NAME_RE.fullmatch(value):
        raise MailConfigError(f"{where} must be 1-24 lowercase letters, digits or dashes")
    return value


def _host(value: object, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise MailConfigError(f"{where} must be a host name")
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    if not _HOST_RE.fullmatch(value) or value.startswith((".", "-")) or ".." in value:
        raise MailConfigError(f"{where} must be a host name (no scheme, path, port or user)")
    return value


def _folder(value: object, where: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 100:
        raise MailConfigError(f"{where} must be a folder name of 1-100 characters")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise MailConfigError(f"{where} must not contain control characters")
    return value


def _ca_file(value: object, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 4096 or "\0" in value or not os.path.isabs(value):
        raise MailConfigError(f"{where} must be an absolute path or null")
    return value


def normalise_calendar_url(value: object, where: str) -> str:
    """https URL of a calendar feed (webcal:// becomes https://). No userinfo, no fragment."""
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        raise MailConfigError(f"{where} must be an https URL")
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in value):
        raise MailConfigError(f"{where} must not contain spaces or control characters")
    url = value
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError:
        raise MailConfigError(f"{where} is not a valid URL") from None
    if parts.scheme.lower() != "https":
        raise MailConfigError(f"{where} must use https")
    if not host or parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise MailConfigError(f"{where} must name a host and no user or password")
    if parts.fragment:
        raise MailConfigError(f"{where} must not have a fragment")
    return url


def _account(obj: object, index: int) -> MailAccount:
    where = f"accounts[{index}]"
    data = _keys(obj, _ACCOUNT_KEYS, where, ("name", "host", "user", "password"))
    name = _name(data["name"], f"{where}.name")
    host = _host(data["host"], f"{where}.host")
    port = data.get("port", 993)
    if type(port) is not int or not 1 <= port <= 65535:
        raise MailConfigError(f"{where}.port must be a number between 1 and 65535")
    user = data["user"]
    # imaplib sends commands as ASCII: a non-ASCII user name would fail at login.
    if not isinstance(user, str) or not 1 <= len(user) <= 256 or not _PRINTABLE_ASCII.fullmatch(user):
        raise MailConfigError(f"{where}.user must be 1-256 printable ASCII characters")
    password = data["password"]
    if not isinstance(password, str) or not 1 <= len(password) <= 1024 or not _PRINTABLE_ASCII.fullmatch(password):
        raise MailConfigError(f"{where}.password must be printable ASCII (use an app password)")
    raw_folders = data.get("folders", ["INBOX"])
    if not isinstance(raw_folders, list) or not 1 <= len(raw_folders) <= MAX_FOLDERS:
        raise MailConfigError(f"{where}.folders must be a list of 1-{MAX_FOLDERS} folder names")
    folders = tuple(_folder(f, f"{where}.folders[{i}]") for i, f in enumerate(raw_folders))
    if len(set(folders)) != len(folders):
        raise MailConfigError(f"{where}.folders has a folder twice")
    address = data.get("address")
    if address is not None and (not isinstance(address, str) or len(address) > 254
                                or not ADDRESS_RE.fullmatch(address)):
        raise MailConfigError(f"{where}.address must be a plain address like name@example.org")
    drafts = data.get("drafts_folder")
    if drafts is not None:
        drafts = _folder(drafts, f"{where}.drafts_folder")
    allow = data.get("allow_draft_append", False)
    if type(allow) is not bool:
        raise MailConfigError(f"{where}.allow_draft_append must be true or false")
    if allow and (address is None or drafts is None):
        raise MailConfigError(f"{where}.allow_draft_append needs address and drafts_folder")
    return MailAccount(name=name, host=host, port=port, user=user, password=password, folders=folders,
                       address=address, drafts_folder=drafts, allow_draft_append=allow,
                       ca_file=_ca_file(data.get("ca_file"), f"{where}.ca_file"))


def _calendar(obj: object, index: int) -> CalendarSource:
    where = f"calendars[{index}]"
    data = _keys(obj, _CALENDAR_KEYS, where, ("name", "url"))
    return CalendarSource(name=_name(data["name"], f"{where}.name"),
                          url=normalise_calendar_url(data["url"], f"{where}.url"),
                          ca_file=_ca_file(data.get("ca_file"), f"{where}.ca_file"))


def parse_connector_config(raw: bytes) -> ConnectorConfig:
    """Validate the file content. Raises MailConfigError (key paths only, never values)."""
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except MailConfigError:
        raise
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise MailConfigError("the mail config file is not valid JSON") from None
    data = _keys(data, _TOP_KEYS, "the mail config", ("version",))
    if type(data["version"]) is not int or data["version"] != CONFIG_VERSION:
        raise MailConfigError(f"version must be {CONFIG_VERSION}")
    raw_accounts = data.get("accounts", [])
    raw_calendars = data.get("calendars", [])
    if not isinstance(raw_accounts, list) or len(raw_accounts) > MAX_ACCOUNTS:
        raise MailConfigError(f"accounts must be a list of at most {MAX_ACCOUNTS} accounts")
    if not isinstance(raw_calendars, list) or len(raw_calendars) > MAX_CALENDARS:
        raise MailConfigError(f"calendars must be a list of at most {MAX_CALENDARS} calendars")
    accounts = tuple(_account(obj, i) for i, obj in enumerate(raw_accounts))
    calendars = tuple(_calendar(obj, i) for i, obj in enumerate(raw_calendars))
    if not accounts and not calendars:
        raise MailConfigError("the mail config names no account and no calendar")
    if len({a.name for a in accounts}) != len(accounts):
        raise MailConfigError("accounts: two accounts have the same name")
    if len({c.name for c in calendars}) != len(calendars):
        raise MailConfigError("calendars: two calendars have the same name")
    return ConnectorConfig(accounts=accounts, calendars=calendars)


def load_connector_config(path: Path) -> ConnectorConfig:
    """Read and validate the config file. Worker process only. Raises MailConfigError."""
    try:
        fd = os.open(path, _READ_FLAGS)
    except OSError:
        raise MailConfigError(FILE_PROBLEM) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise MailConfigError(FILE_PROBLEM)
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise MailConfigError(FILE_PROBLEM)
        if info.st_size > MAX_CONFIG_BYTES:
            raise MailConfigError("the mail config file is too large")
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_CONFIG_BYTES:
            chunk = os.read(fd, MAX_CONFIG_BYTES + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    except OSError:
        raise MailConfigError(FILE_PROBLEM) from None
    finally:
        os.close(fd)
    raw = b"".join(chunks)
    if len(raw) > MAX_CONFIG_BYTES:
        raise MailConfigError("the mail config file is too large")
    return parse_connector_config(raw)


__all__ = [
    "ADDRESS_RE", "CalendarSource", "ConnectorConfig", "MAX_ACCOUNTS", "MAX_CALENDARS", "MAX_CONFIG_BYTES",
    "MAX_FOLDERS", "MailAccount", "MailConfigError", "NAME_RE", "file_problem", "imap_utf7",
    "load_connector_config", "normalise_calendar_url", "parse_connector_config", "quote_astring", "quote_mailbox",
]
