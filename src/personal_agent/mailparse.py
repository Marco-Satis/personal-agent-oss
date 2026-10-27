"""Reading mail safely: headers, addresses, MIME bodies and HTML as plain, inert text.

Pure functions without any network access. The mail worker uses them on raw IMAP
data; the parent process uses the cleaners again on every string the worker
returns (a parser bug in the worker must not pass escape sequences through).

Everything a mail contains is untrusted text written by a stranger:

- headers are decoded (RFC 2047) from a capped raw value, never with a full parser
  over unbounded input, and cleaned to one line;
- an address is accepted only when exactly one plain ASCII address parses, so
  forms such as '"a@good.example"@evil.example' or two addresses in one field give
  no address at all (and therefore no draft can ever be addressed from them);
- the body prefers text/plain; HTML is reduced to text, and the content of
  hidden elements (a classic way to hide instructions) is dropped and counted;
- links keep only their host: '[link: example.org]'. Paths, queries, reset tokens
  and tracking ids never leave the worker, and long token-like runs become
  '[code]';
- control characters (including ESC, so no terminal escape sequence reaches a
  console), invisible characters, line/paragraph separators and combining-mark
  floods are removed or capped.

The `injection_signals` codes are computed by code from the mail, shown to the
person, and never derived from model output.
"""

from __future__ import annotations

import codecs
import hashlib
import html.parser
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email import message_from_bytes
from email.header import decode_header
from email.message import Message
from email.policy import compat32
from email.utils import getaddresses, parsedate_tz, mktime_tz
from typing import Callable, Container
from urllib.parse import urlsplit

from .mailconfig import ADDRESS_RE
from .textsafe import is_invisible, strip_invisible

MAX_HEADER_FIELD = 4096          # raw bytes of one header field before any decoding
MAX_HEADER_BLOCK = 32 * 1024     # raw header block of one message
MAX_DECODED_BYTES = 1024 * 1024  # decoded payload of one body part
MAX_HTML_INPUT = 200_000         # characters of HTML fed to the parser
HTML_CHUNK = 20_000
MAX_PARTS = 200
MAX_DEPTH = 10
MAX_COMBINING = 2                # combining marks kept per base character
UNREADABLE = "(unreadable)"

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_MONTH_INDEX = {name.lower(): index + 1 for index, name in enumerate(MONTHS)}

SIGNAL_TEXTS = {
    "hidden_text": "hidden text removed",
    "invisible": "invisible characters removed",
    "instructions": "text addressed to an assistant",
    "name_domain": "sender name shows another domain",
    "link_mismatch": "link text and target differ",
    "mixed_script": "mixed alphabets in a name or link",
    "idn": "international domain name",
    "reply_to_differs": "replies would go to another domain",
}
SIGNAL_CODES = tuple(SIGNAL_TEXTS)

_SPACES = re.compile(r"[ \t\f\v\r\n]+")
_BLANK_RUNS = re.compile(r"\n{4,}")
_DOMAINISH = re.compile(r"(?<![\w.-])(?:[\w-]+\.)+(?:[^\W\d_]{2,}|xn--[\w-]+)(?![\w-])", re.UNICODE)
_INSTRUCTIONS = re.compile(
    r"ignore (?:all |any )?(?:previous|prior|above|earlier) (?:instructions|messages|rules)"
    r"|disregard (?:all |any )?(?:previous|prior|above) "
    r"|system prompt|you are an ai\b|you are a language model|assistant:|<\s*/?\s*untrusted"
    r"|new instructions:|developer message|ignoriere (?:alle )?(?:vorherigen|bisherigen) anweisungen",
    re.IGNORECASE,
)
# URLs in plain text: scheme://..., www...., and bare domain/path forms.
_URL_SCHEME = re.compile(r"\b[a-z][a-z0-9+.-]{1,15}://[^\s<>\"'\]\[)(]+", re.IGNORECASE)
_URL_WWW = re.compile(r"\bwww\.[^\s<>\"'\]\[)(]+", re.IGNORECASE)
_URL_BARE = re.compile(
    r"(?<![\w@.-])(?:[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\.)+[a-z]{2,24}(?=[/?#])[^\s<>\"'\]\[)(]*",
    re.IGNORECASE,
)
_TOKEN = re.compile(r"(?<![A-Za-z0-9_+/=-])[A-Za-z0-9_+/=-]{20,}(?![A-Za-z0-9_+/=-])")
_PLACEHOLDER = re.compile("\x00(\\d+)\x00")
_CODE_KEYWORDS = re.compile(
    r"code|verification|verify|otp|tan\b|pin\b|passcode|password|kennwort|bestätigungscode|sicherheitscode"
    r"|einmal|anmeldecode|sicherheits",
    re.IGNORECASE,
)
_CODE_RUN = re.compile(r"(?<![\d.,:/-])\d{4,8}(?![\d.,:/-]?\d)")
_INTERNALDATE = re.compile(
    r"\s*(\d{1,2})-([A-Za-z]{3})-(\d{4}) (\d{2}):(\d{2}):(\d{2}) ([+-])(\d{2})(\d{2})\s*")
_REPLY_PREFIX = re.compile(r"^(?:\s*(?:re|fwd?|aw|wg|sv|vs|tr)\s*(?:\[\d+\])?\s*:\s*)+", re.IGNORECASE)


@dataclass(frozen=True)
class Address:
    name: str
    addr: str | None
    idn: bool = False
    mixed_script: bool = False


@dataclass(frozen=True)
class BodyText:
    text: str
    source: str               # "plain" | "html" | "none"
    truncated: bool
    attachments: int
    hidden_dropped: int
    signals: tuple[str, ...]


# --- single lines and blocks -----------------------------------------------------------


def _limit_combining(text: str) -> str:
    """At most MAX_COMBINING combining marks after each base character (no 'Zalgo' floods)."""
    if text.isascii():
        return text
    out: list[str] = []
    run = 0
    for ch in text:
        if unicodedata.category(ch) in ("Mn", "Me"):
            run += 1
            if run > MAX_COMBINING:
                continue
        else:
            run = 0
        out.append(ch)
    return "".join(out)


def _separators(text: str, replacement: str) -> str:
    return text.replace(" ", replacement).replace(" ", replacement).replace("\x85", replacement)


def count_invisible(text: str) -> int:
    """How many invisible (not control) characters text contains."""
    if text.isascii():
        return 0
    return sum(1 for ch in text if not ch.isascii() and is_invisible(ch))


def clean_line(value: object, limit: int) -> str:
    """Untrusted text as one clean line of at most `limit` characters."""
    text = str(value) if value is not None else ""
    text = _separators(text, " ")
    text = strip_invisible(text, keep="\t\n\r")
    text = _limit_combining(text)
    text = _SPACES.sub(" ", text).strip()
    if len(text) > limit:
        text = text[: max(1, limit - 1)].rstrip() + "…"
    return text


def clean_block(text: str, max_chars: int) -> tuple[str, bool]:
    """Untrusted multi-line text: LF line ends, no control/invisible characters except tab,
    no trailing spaces, at most two blank lines in a row, cut at max_chars (on a line
    boundary when one is near). Returns (text, truncated)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _separators(text, "\n")
    text = strip_invisible(text, keep="\n\t")
    text = _limit_combining(text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = _BLANK_RUNS.sub("\n\n\n", text).strip("\n")
    if len(text) <= max_chars:
        return text, False
    cut = text.rfind("\n", 0, max_chars + 1)
    if cut < max_chars // 2:
        cut = max_chars
    return text[:cut].rstrip(), True


def display_subject(subject: str, limit: int) -> str:
    """The subject with repeated reply/forward prefixes collapsed to one."""
    text = clean_line(subject, 400)
    match = _REPLY_PREFIX.match(text)
    if match and match.group(0).count(":") > 1:
        first = match.group(0).split(":", 1)[0].strip()
        text = f"{first}: {text[match.end():]}"
    return clean_line(text, limit)


# --- links and codes -------------------------------------------------------------------


def link_host(url: str) -> str | None:
    """The lowercase, IDNA-encoded host of a URL (userinfo and port dropped), or None."""
    try:
        host = urlsplit(url.strip()).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.strip(".").lower()
    if not host or len(host) > 253:
        return None
    try:
        encoded = host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return None
    return encoded.lower()


def _host_marker(raw: str) -> str:
    text = raw if "://" in raw else "https://" + raw
    host = link_host(text)
    return f"[link: {host}]" if host else "[link]"


def reduce_links(text: str) -> str:
    """Every URL becomes '[link: <host>]' and every long token-like run with a digit '[code]'."""
    saved: list[str] = []

    def keep(marker: str) -> str:
        saved.append(marker)
        return f"\x00{len(saved) - 1}\x00"

    text = _URL_SCHEME.sub(lambda m: keep(_host_marker(m.group())), text)
    text = _URL_WWW.sub(lambda m: keep(_host_marker(m.group())), text)
    text = _URL_BARE.sub(lambda m: keep(_host_marker(m.group())), text)
    text = _TOKEN.sub(lambda m: "[code]" if any(ch.isdigit() for ch in m.group()) else m.group(), text)
    return _PLACEHOLDER.sub(lambda m: saved[int(m.group(1))], text)


def mask_codes(text: str) -> str:
    """4-8 digit runs within 40 characters of a word like 'code', 'TAN' or 'PIN' become
    '[code]' (one-time codes never go to a model or a phone notice)."""
    keywords = [m.span() for m in _CODE_KEYWORDS.finditer(text)]
    if not keywords:
        return text

    def near(start: int, end: int) -> bool:
        return any(k_start - 40 <= end and start <= k_end + 40 for k_start, k_end in keywords)

    return _CODE_RUN.sub(lambda m: "[code]" if near(*m.span()) else m.group(), text)


# --- scripts and domains -----------------------------------------------------------------


def _script(ch: str) -> str | None:
    if not ch.isalpha():
        return None
    if ch.isascii():
        return "latin"
    try:
        name = unicodedata.name(ch)
    except ValueError:
        return None
    for script in ("LATIN", "CYRILLIC", "GREEK"):
        if name.startswith(script):
            return script.lower()
    return None


def mixed_script(text: str) -> bool:
    """True when one word mixes Latin letters with Cyrillic or Greek ones (look-alike names)."""
    if text.isascii():
        return False
    for token in re.findall(r"[\w.-]+", text):
        scripts = {_script(ch) for ch in token} - {None}
        if "latin" in scripts and scripts & {"cyrillic", "greek"}:
            return True
    return False


def domain_of(addr: str | None) -> str | None:
    if not addr or "@" not in addr:
        return None
    return addr.rsplit("@", 1)[1].lower()


def _same_site(a: str, b: str) -> bool:
    a, b = a.lower().removeprefix("www."), b.lower().removeprefix("www.")
    return a == b or a.endswith("." + b) or b.endswith("." + a)


# --- headers -------------------------------------------------------------------------------


def _decode_raw(value: bytes) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return value.decode("latin-1")


def decode_words(raw: str | bytes | None, limit: int) -> str:
    """An RFC 2047 header value as one clean line. The raw value is capped first."""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        raw = _decode_raw(raw[:MAX_HEADER_FIELD])
    raw = raw[:MAX_HEADER_FIELD]
    if "=?" not in raw:
        return clean_line(raw, limit)
    parts: list[str] = []
    try:
        for chunk, charset in decode_header(raw):
            if isinstance(chunk, str):
                parts.append(chunk)
                continue
            if charset is None:
                try:
                    parts.append(chunk.decode("raw-unicode-escape"))
                except UnicodeDecodeError:
                    parts.append(chunk.decode("latin-1"))
                continue
            name = charset.lower()
            try:
                codecs.lookup(name)
            except LookupError:
                name = "latin-1"
            parts.append(chunk.decode(name, "replace"))
    except Exception:  # noqa: BLE001 - a broken header is shown raw, never fatal
        return clean_line(raw, limit)
    return clean_line("".join(parts), limit)


def parse_header_block(raw: bytes) -> dict[str, str]:
    """Header fields of a raw header block (lowercase name -> raw value of its first
    occurrence), unfolded, each value capped at MAX_HEADER_FIELD. Linear in the input."""
    block = raw[:MAX_HEADER_BLOCK]
    end = block.find(b"\r\n\r\n")
    if end < 0:
        end = block.find(b"\n\n")
    if end >= 0:
        block = block[:end]
    fields: dict[str, list[str]] = {}
    current: list[str] | None = None
    size = 0
    for line in block.split(b"\n"):
        line = line.rstrip(b"\r")
        if not line:
            continue
        if line[:1] in (b" ", b"\t"):
            if current is not None and size < MAX_HEADER_FIELD:
                text = _decode_raw(line)
                current.append(text)
                size += len(text)
            continue
        name, sep, value = line.partition(b":")
        if not sep or not re.fullmatch(rb"[!-9;-~]{1,76}", name):
            current = None
            continue
        key = name.decode("ascii").lower()
        if key in fields:
            current = None  # only the first occurrence counts (a second From is ignored)
            continue
        text = _decode_raw(value)[:MAX_HEADER_FIELD]
        current = [text]
        size = len(text)
        fields[key] = current
    return {key: " ".join(part.strip() for part in parts)[:MAX_HEADER_FIELD] for key, parts in fields.items()}


def _structure_counts(raw: str) -> tuple[int, int]:
    """('@' and ',' outside quotes, comments and nothing else) in an address header."""
    at = commas = 0
    quoted = False
    depth = 0
    escape = False
    for ch in raw:
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if quoted:
            if ch == '"':
                quoted = False
            continue
        if ch == '"':
            quoted = True
        elif ch == "(":
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
        elif depth:
            continue
        elif ch == "@":
            at += 1
        elif ch == ",":
            commas += 1
    return at, commas


def _plain_address(addr: str) -> tuple[str | None, bool]:
    """(validated lowercase address, idn) or (None, False)."""
    addr = addr.strip()
    if "@" not in addr:
        return None, False
    local, _, domain = addr.rpartition("@")
    idn = False
    if not domain.isascii():
        try:
            domain = domain.encode("idna").decode("ascii")
        except (UnicodeError, ValueError):
            return None, False
        idn = True
    candidate = f"{local}@{domain}"
    if len(candidate) > 254 or not ADDRESS_RE.fullmatch(candidate):
        return None, False
    if any(label.startswith("xn--") for label in domain.lower().split(".")):
        idn = True
    return candidate.lower(), idn


def parse_address(raw: str | None) -> Address:
    """One sender-like address. addr is None unless exactly one plain address parses."""
    if not raw:
        return Address("", None)
    raw = raw[:MAX_HEADER_FIELD]
    at, commas = _structure_counts(raw)
    try:
        parsed = getaddresses([raw])
    except Exception:  # noqa: BLE001
        parsed = []
    name_raw = parsed[0][0] if len(parsed) == 1 else ""
    addr: str | None = None
    idn = False
    if at == 1 and commas == 0 and len(parsed) == 1:
        addr, idn = _plain_address(parsed[0][1])
    name = decode_words(name_raw, 60) if name_raw else ""
    if not name and addr is None:
        name = decode_words(raw, 60)
    mixed = mixed_script(name)
    return Address(name=name, addr=addr, idn=idn, mixed_script=mixed)


def count_addresses(raw: str | None) -> int:
    """How many addresses a To/Cc field names (at most 100). A count only, never used to send."""
    if not raw:
        return 0
    try:
        return min(100, sum(1 for _, addr in getaddresses([raw[:MAX_HEADER_FIELD]]) if "@" in addr))
    except Exception:  # noqa: BLE001
        return 0


def _date_header(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        parsed = parsedate_tz(raw[:200])
        return float(mktime_tz(parsed)) if parsed else None
    except (TypeError, ValueError, OverflowError):
        return None


MESSAGE_ID_RE = re.compile(r"<[^<>\s]{1,250}>")


def parse_headers(raw: bytes) -> dict:
    """The allowlisted, cleaned fields of a raw header block."""
    fields = parse_header_block(raw)
    sender = parse_address(fields.get("from"))
    reply_to = parse_address(fields.get("reply-to")) if fields.get("reply-to") else Address("", None)
    precedence = fields.get("precedence", "").strip().lower()
    auto = fields.get("auto-submitted", "").strip().lower()
    message_id = fields.get("message-id", "").strip()
    return {
        "from": sender,
        "reply_to": reply_to,
        "subject": decode_words(fields.get("subject"), 200),
        "date": _date_header(fields.get("date")),
        "to_count": count_addresses(fields.get("to")),
        "cc_count": count_addresses(fields.get("cc")),
        "list": bool(fields.get("list-id") or fields.get("list-unsubscribe")) or precedence in ("list", "bulk"),
        "auto": bool(auto and auto != "no") or precedence in ("bulk", "junk", "auto_reply"),
        "message_id_ok": bool(MESSAGE_ID_RE.fullmatch(message_id)),
    }


# --- HTML -----------------------------------------------------------------------------------

_DROP_TAGS = frozenset(("script", "style", "head", "title", "template", "noscript", "svg", "math", "iframe",
                        "object"))
_VOID_TAGS = frozenset(("br", "img", "hr", "meta", "link", "input", "wbr", "area", "base", "col", "embed",
                        "source", "track", "param"))
_BLOCK_TAGS = frozenset(("p", "div", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote", "ul",
                         "ol", "section", "article", "header", "footer", "pre", "center"))
_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?![.\d]*[1-9])|opacity\s*:\s*0(?![.\d]*[1-9])"
    r"|max-height\s*:\s*0(?![.\d]*[1-9])",
    re.IGNORECASE,
)


class _TextExtractor(html.parser.HTMLParser):
    def __init__(self, max_chars: int) -> None:
        super().__init__(convert_charrefs=True)
        self.max_chars = max_chars
        self.out: list[str] = []
        self.size = 0
        self.full = False
        self.stack: list[tuple[str, bool]] = []
        self.hidden_depth = 0
        self.hidden_dropped = 0
        self.link_mismatch = False
        self.mixed_link = False
        self.anchor: tuple[str | None, int] | None = None  # (href, index into out)

    def _emit(self, text: str) -> None:
        if self.full or self.hidden_depth:
            return
        room = self.max_chars - self.size
        if len(text) >= room:
            text = text[:room]
            self.full = True
        self.out.append(text)
        self.size += len(text)

    @staticmethod
    def _hidden(tag: str, attrs: list[tuple[str, str | None]]) -> bool:
        if tag in _DROP_TAGS:
            return True
        for name, value in attrs:
            if name == "hidden" or (name == "aria-hidden" and (value or "").lower() == "true"):
                return True
            if name == "style" and value and _HIDDEN_STYLE.search(value):
                return True
        return False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _VOID_TAGS:
            if tag == "br":
                self._emit("\n")
            elif tag == "hr":
                self._emit("\n")
            elif tag == "img" and not self._hidden(tag, attrs):
                alt = next((v for k, v in attrs if k == "alt" and v), "")
                alt = clean_line(alt, 40)
                self._emit(f"[image: {alt}]" if alt else "[image]")
            return
        hidden = self._hidden(tag, attrs)
        if hidden:
            if not self.hidden_depth and tag not in _DROP_TAGS:
                self.hidden_dropped += 1
            self.hidden_depth += 1
        self.stack.append((tag, hidden))
        if tag in _BLOCK_TAGS:
            self._emit("\n")
        elif tag == "li":
            self._emit("\n- ")
        elif tag in ("td", "th"):
            self._emit(" ")
        elif tag == "a" and not self.hidden_depth:
            href = next((v for k, v in attrs if k == "href"), None)
            self.anchor = (href, len(self.out))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _VOID_TAGS:
            self.handle_starttag(tag, attrs)
        elif tag in _BLOCK_TAGS:
            self._emit("\n")

    def handle_endtag(self, tag: str) -> None:
        if not any(open_tag == tag for open_tag, _ in self.stack):
            return
        while self.stack:
            open_tag, hidden = self.stack.pop()
            if hidden:
                self.hidden_depth -= 1
            if open_tag == "a":
                self._close_anchor()
            if open_tag == tag:
                break
        if tag in _BLOCK_TAGS:
            self._emit("\n")

    def _close_anchor(self) -> None:
        if self.anchor is None:
            return
        href, start = self.anchor
        self.anchor = None
        if self.hidden_depth or not href:
            return
        href = href.strip()
        if href.startswith("#"):
            return
        visible = "".join(self.out[start:])
        if href.lower().startswith("mailto:"):
            self._emit(" [address]")
            return
        host = link_host(href) if re.match(r"(?i)https?://", href) else None
        if host is None:
            self._emit(" [link]")
            return
        if mixed_script(host) or host.startswith("xn--") or ".xn--" in host:
            self.mixed_link = True
        for shown in _DOMAINISH.findall(visible):
            shown_host = link_host("https://" + shown)
            if shown_host and not _same_site(shown_host, host):
                self.link_mismatch = True
        self._emit(f" [link: {host}]")

    def handle_data(self, data: str) -> None:
        self._emit(data)


def html_to_text(source: str, max_chars: int, deadline: Callable[[], bool] | None = None) -> tuple[str, int]:
    """(text, hidden_dropped) of an HTML body. At most MAX_HTML_INPUT characters are fed,
    in chunks; deadline() returning True stops early."""
    text, hidden, _, _ = _html_to_text(source, max_chars, deadline)
    return text, hidden


def _html_to_text(source: str, max_chars: int,
                  deadline: Callable[[], bool] | None = None) -> tuple[str, int, bool, bool]:
    parser = _TextExtractor(max_chars)
    source = source[:MAX_HTML_INPUT]
    try:
        for start in range(0, len(source), HTML_CHUNK):
            if parser.full or (deadline is not None and deadline()):
                break
            parser.feed(source[start:start + HTML_CHUNK])
        if not parser.full:
            parser.close()
    except Exception:  # noqa: BLE001 - keep what was collected
        pass
    return "".join(parser.out), parser.hidden_dropped, parser.link_mismatch, parser.mixed_link


# --- bodies -----------------------------------------------------------------------------------


def _decode_payload(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        payload = raw.encode("utf-8", "replace") if isinstance(raw, str) else b""
    if not isinstance(payload, bytes):
        return ""
    payload = payload[:MAX_DECODED_BYTES]
    charset = part.get_content_charset() or "utf-8"
    try:
        codecs.lookup(charset)
    except LookupError:
        charset = "utf-8"
    try:
        text = payload.decode(charset, "replace")
    except (LookupError, ValueError):
        text = payload.decode("utf-8", "replace")
    if text.count("�") > max(10, len(text) // 100):
        text = payload.decode("cp1252", "replace")
    return text


def _is_attachment(part: Message) -> bool:
    disposition = str(part.get("Content-Disposition", "") or "").strip().lower()
    if disposition.startswith("attachment"):
        return True
    try:
        return bool(part.get_filename())
    except Exception:  # noqa: BLE001
        return False


def injection_signals(text: str, hidden_dropped: int, invisible_removed: int, from_: Address,
                      extra: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Fixed signal codes, in SIGNAL_CODES order."""
    found = set(extra)
    if hidden_dropped > 0:
        found.add("hidden_text")
    if invisible_removed > 0:
        found.add("invisible")
    if _INSTRUCTIONS.search(text or ""):
        found.add("instructions")
    domain = domain_of(from_.addr)
    if domain and from_.name:
        for shown in _DOMAINISH.findall(from_.name):
            if not _same_site(shown.lower(), domain):
                found.add("name_domain")
    if from_.mixed_script:
        found.add("mixed_script")
    if from_.idn:
        found.add("idn")
    return tuple(code for code in SIGNAL_CODES if code in found)


def extract_body(raw: bytes, max_chars: int = 20_000, declared_size: int | None = None,
                 deadline: Callable[[], bool] | None = None) -> BodyText:
    """The readable text of a raw message: first text/plain, else the first text/html as
    text. Attachments and attached messages are counted, never shown."""
    try:
        message = message_from_bytes(raw, policy=compat32)
    except Exception:  # noqa: BLE001
        return BodyText("(could not read this mail)", "none", False, 0, 0, ())
    attachments = 0
    plain: Message | None = None
    html_part: Message | None = None
    stack: list[tuple[Message, int]] = [(message, 0)]
    seen = 0
    while stack and seen < MAX_PARTS:
        part, depth = stack.pop()
        seen += 1
        ctype = (part.get_content_type() or "").lower()
        if ctype == "message/rfc822":
            attachments += 1
            continue
        if part.is_multipart():
            if depth < MAX_DEPTH:
                children = part.get_payload()
                if isinstance(children, list):
                    stack.extend((child, depth + 1) for child in reversed(children)
                                 if isinstance(child, Message))
            continue
        if part is not message and _is_attachment(part):
            attachments += 1
            continue
        if ctype == "text/plain":
            if plain is None:  # a Message with no headers is falsy: never use `or` here
                plain = part
        elif ctype == "text/html":
            if html_part is None:
                html_part = part
        elif part is not message:
            attachments += 1  # inline images, calendars, other parts: counted, never shown
    hidden = 0
    link_mismatch = mixed_link = False
    if plain is not None:
        source, text = "plain", _decode_payload(plain)
    elif html_part is not None:
        source = "html"
        text, hidden, link_mismatch, mixed_link = _html_to_text(_decode_payload(html_part), max_chars * 2, deadline)
    else:
        return BodyText("(no readable text)", "none", False, attachments, 0, ())
    invisible = count_invisible(text)
    text = reduce_links(text)
    text, truncated = clean_block(text, max_chars)
    if declared_size is not None and len(raw) < declared_size:
        truncated = True
    extra = tuple(code for code, on in (("link_mismatch", link_mismatch), ("mixed_script", mixed_link)) if on)
    signals = injection_signals(text, hidden, invisible, Address("", None), extra)
    return BodyText(text, source, truncated, attachments, hidden, signals)


def strip_quoted(text: str, keep: int = 20) -> str:
    """At most `keep` quoted ('>') lines are kept; the rest is replaced by one note."""
    out: list[str] = []
    quoted = dropped = 0
    for line in text.split("\n"):
        if line.lstrip().startswith(">"):
            quoted += 1
            if quoted > keep:
                dropped += 1
                continue
        out.append(line)
    if dropped:
        out.append(f"[{dropped} quoted lines removed]")
    return "\n".join(out)


# --- dates and refs ----------------------------------------------------------------------------


def parse_internaldate(raw: str | bytes | None) -> float | None:
    """'17-Jul-1996 02:44:25 -0700' as an epoch, without the locale (English month names)."""
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("ascii", "replace")
    match = _INTERNALDATE.fullmatch(raw)
    if not match:
        return None
    day, mon, year, hh, mm, ss, sign, oh, om = match.groups()
    month = _MONTH_INDEX.get(mon.lower())
    if month is None:
        return None
    try:
        offset = timedelta(hours=int(oh), minutes=int(om))
        tz = timezone(offset if sign == "+" else -offset)
        return datetime(int(year), month, int(day), int(hh), int(mm), int(ss), tzinfo=tz).timestamp()
    except (ValueError, OverflowError):
        return None


def imap_date(day: date) -> str:
    """The IMAP SEARCH date ('07-Oct-2026'); never strftime('%b'), which follows the locale."""
    return f"{day.day:02d}-{MONTHS[day.month - 1]}-{day.year:04d}"


def message_ref(account: str, folder: str, uidvalidity: int, uid: int, taken: Container[str]) -> str:
    """A short local id: 6 hex characters of a digest of the message key; on a collision
    with `taken`, the next 6-hex window."""
    digest = hashlib.sha256(f"{account}\0{folder}\0{uidvalidity}\0{uid}".encode("utf-8")).hexdigest()
    for start in range(0, len(digest) - 5, 6):
        ref = digest[start:start + 6]
        if ref not in taken:
            return ref
    for value in range(16 ** 6):  # practically unreachable
        ref = f"{value:06x}"
        if ref not in taken:
            return ref
    raise ValueError("no free mail id")


__all__ = [
    "Address", "BodyText", "MAX_HEADER_BLOCK", "MAX_HEADER_FIELD", "SIGNAL_CODES", "SIGNAL_TEXTS", "UNREADABLE",
    "clean_block", "clean_line", "count_addresses", "count_invisible", "decode_words", "display_subject",
    "domain_of", "extract_body", "html_to_text", "imap_date", "injection_signals", "link_host", "mask_codes",
    "message_ref", "mixed_script", "parse_address", "parse_header_block", "parse_headers", "parse_internaldate",
    "reduce_links", "strip_quoted",
]
