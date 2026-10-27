"""The notification layer: one place that sends notices to the configured channels.

Kinds of notices:

  brief      the morning brief (rendered per channel by brief.py)
  alert      an escalation event and whether it calls (server.py)
  approval   a remote approval notice (remote.ApprovalInbox)
  task_done  task counts, optionally ids and titles (coordinator.TaskNotifier)
  info       fixed operational notices (e.g. the morning brief gave up for today)

PA_NOTIFY_<KIND> picks the channels: a comma list of `telegram`, `discord`, or
`none`. Unset, a kind goes to Telegram when a paired Telegram bot is ready, and
otherwise to Discord as before: the brief when PA_DISCORD_WEBHOOK is set,
approvals when the webhook and PA_PUBLIC_APPROVAL_URL are set, task counts only
with PA_TASK_DISCORD=counts. Alerts and info notices are opt-in on Discord.

Privacy is enforced by the renderers below, not by callers: the approval
renderer has no parameter for the tool input, the task renderers take counts
(or an id, a status and a cleaned title) and never results, errors or paths,
and info notices are fixed templates. Every untrusted field goes through the
channel's own `inert` function (no markup, no mentions, defanged URLs).

Each channel declares its message limit and how many chunks one notice may use;
longer text is split at line boundaries and cut with a note. Nothing here ever
logs the text of a notice.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from typing import TYPE_CHECKING, Callable, Protocol

from .approval import LEVEL_LABELS, Level
from .config import NOTIFY_KINDS, NOTIFY_TARGETS
from .status import clean_text
from .textsafe import strip_invisible

if TYPE_CHECKING:
    from .config import Config
    from .escalation import Event

log = logging.getLogger(__name__)

KINDS = NOTIFY_KINDS
TARGET_NAMES = NOTIFY_TARGETS
ASYNC_KINDS = ("alert", "info", "task_done")

DISCORD_LIMIT = 1900          # Discord allows 2000 characters per message; kept with margin
DISCORD_MAX_CHUNKS = 5
TELEGRAM_API_LIMIT = 4096     # UTF-16 code units per Telegram message
TELEGRAM_LIMIT = 4000         # kept with margin
TELEGRAM_MAX_CHUNKS = 5
CHUNK_GAP_S = 0.6             # between two chunks of one notice
QUEUE_SIZE = 50
RATE_WINDOW_S = 10 * 60       # alert and info: at most RATE_LIMIT per window ...
RATE_LIMIT = 10
KEY_WINDOW_S = 60 * 60        # ... and the same key at most once per hour
REASON_LOG_EVERY_S = 10 * 60
OVERFLOW_NOTE = "\n… (cut)"

DISCORD_MARKUP = re.compile(r"([\\_*~|`>#\[\]()-])")
_SPACES = re.compile(r"\s+")
_TASK_ID = re.compile(r"^[0-9a-f]{6}$")
_TASK_STATES = ("done", "failed", "cancelled")


# --- text helpers --------------------------------------------------------------


def utf16_len(text: str) -> int:
    """Length in UTF-16 code units (the unit of Telegram's message limit)."""
    return len(text.encode("utf-16-le")) // 2


def defang(text: str) -> str:
    """'https://x' becomes 'https[:]//x', so no client shows a link."""
    return text.replace("://", "[:]//")


def discord_safe(text: object, limit: int = 60) -> str:
    """Plain text for a Discord message: no mentions, no markup (`mcp__a__b` would underline)."""
    return DISCORD_MARKUP.sub(r"\\\1", clean_text(text, limit))


def discord_line(text: object, limit: int = 160) -> str:
    """Untrusted text (notes, status files, model output) as one inert Discord line."""
    return defang(discord_safe(text, limit))


def plain_line(text: object, limit: int = 160) -> str:
    """Untrusted text as one inert line of plain text (Telegram sends no markup at all)."""
    flat = _SPACES.sub(" ", strip_invisible(str(text) if text is not None else "", keep="\n\t")).strip()
    if len(flat) > limit:
        flat = flat[: max(1, limit - 1)].rstrip() + "…"
    return defang(flat)


_TG_COMMAND = re.compile(r"(^|[\s(\[{\"'«“‚„<>|,;:!?])/(?=\w)")
_TG_MENTION = re.compile(r"@(?=[A-Za-z0-9_])")
_TG_DOMAIN = re.compile(r"(?<![\w.-])(?:[\w-]+\.)+(?:[^\W\d_]{2,}|xn--[\w-]+)(?![\w-])", re.UNICODE)


def telegram_untrusted(text: object, limit: int = 160) -> str:
    """Third-party text (mail subjects, senders, bodies, calendar titles) as one inert
    Telegram line.

    Telegram turns '/word' in a bot message into a tappable bot command (a tap sends
    it as the owner), '@name' into a mention and bare domains into links. On top of
    plain_line: '/' at the start of a token becomes U+2215, '@' before a name character
    becomes U+FF20, and the dots of domain-like tokens become '[.]'.
    """
    flat = plain_line(text, limit)
    flat = _TG_COMMAND.sub(lambda m: m.group(1) + "∕", flat)
    flat = _TG_MENTION.sub("＠", flat)
    return _TG_DOMAIN.sub(lambda m: m.group().replace(".", "[.]"), flat)


def channel_limits(name: str) -> tuple[int, int, Callable[[str], int]]:
    """(limit, max_chunks, measure) of a channel."""
    if name == "telegram":
        return TELEGRAM_LIMIT, TELEGRAM_MAX_CHUNKS, utf16_len
    return DISCORD_LIMIT, DISCORD_MAX_CHUNKS, len


def _cut(text: str, budget: int, measure: Callable[[str], int]) -> int:
    """The largest n with measure(text[:n]) <= budget. Indexing is per code point, so a
    character outside the BMP (two UTF-16 units) is never split."""
    if measure(text) <= budget:
        return len(text)
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if measure(text[:mid]) <= budget:
            low = mid
        else:
            high = mid - 1
    return low


def split_message(text: str, limit: int, max_chunks: int, measure: Callable[[str], int] = len,
                  overflow_note: str = OVERFLOW_NOTE, *, keep: str = "\n") -> list[str]:
    """Split at line boundaries into messages of at most `limit` (in the channel's unit).

    A single longer line is hard-split. Beyond max_chunks the last chunk ends with
    overflow_note. Control and invisible characters except those in `keep` (newlines;
    model replies also keep tabs) are removed.
    """
    text = strip_invisible(text, keep=keep if "\n" in keep else keep + "\n")
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while measure(line) > limit:
            if current.strip():
                chunks.append(current)
            current = ""
            n = max(1, _cut(line, limit, measure))
            chunks.append(line[:n])
            line = line[n:]
        candidate = f"{current}\n{line}" if current else line
        if measure(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current.strip():
        chunks.append(current)
    chunks = [chunk.strip("\n") for chunk in chunks if chunk.strip()]
    if len(chunks) > max_chunks:
        chunks = chunks[:max_chunks]
        last = chunks[-1]
        chunks[-1] = last[: _cut(last, max(0, limit - measure(overflow_note)), measure)].rstrip() + overflow_note
    return chunks


# --- channels ------------------------------------------------------------------


class Target(Protocol):
    name: str          # "telegram" | "discord"
    limit: int         # max size of one message in the channel's own unit
    max_chunks: int

    def measure(self, text: str) -> int: ...
    def inert(self, text: object, limit: int = 160) -> str: ...
    def ready(self) -> str | None: ...       # None: can send now; else a short reason
    def post(self, text: str) -> bool: ...   # one message within limit; never raises; never logs text


class DiscordTarget:
    """A Discord webhook as a notification channel (counts by default, escaped text)."""

    name = "discord"
    limit = DISCORD_LIMIT
    max_chunks = DISCORD_MAX_CHUNKS

    def __init__(self, webhook: object) -> None:
        self.webhook = webhook  # anything with .post(text) -> bool, usually remote.DiscordWebhook

    def __repr__(self) -> str:
        return "DiscordTarget(<hidden>)"

    def measure(self, text: str) -> int:
        return len(text)

    def inert(self, text: object, limit: int = 160) -> str:
        return discord_line(text, limit)

    def ready(self) -> str | None:
        return None

    def post(self, text: str) -> bool:
        try:
            return bool(self.webhook.post(text))  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - a notice never raises; the message may hold the URL
            log.warning("discord post failed: %s", type(exc).__name__)
            return False


# --- renderers (the only way text reaches a channel) ---------------------------

NOTICE_TEXTS = {
    "en": {
        "discord": "Approval needed (level {level}): {tool}. Open <{link}> (expires in {minutes} min).",
        "head": "Approval needed · {origin}",
        "head_seq": "Approval #{seq} needed · {origin}",
        "level": "Level {level} ({label}): {tool}",
        "context": "(this request may come from note text or a plugin skill)",
        "expires": "Expires in {minutes} min.",
        "review": "Review: {link}",
    },
    "de": {
        "discord": "Freigabe nötig (Stufe {level}): {tool}. Öffnen: <{link}> (läuft in {minutes} Min. ab).",
        "head": "Freigabe nötig · {origin}",
        "head_seq": "Freigabe #{seq} nötig · {origin}",
        "level": "Stufe {level} ({label}): {tool}",
        "context": "(diese Anfrage kann aus Notiztext oder einem Plugin-Skill stammen)",
        "expires": "Läuft in {minutes} Min. ab.",
        "review": "Prüfen: {link}",
    },
}

ALERT_TEXTS = {
    "en": {"alert": "Alert ({severity}) {key}: {summary} — {outcome}", "calling": "calling you now",
           "no_call": "no call: {reason}"},
    "de": {"alert": "Alarm ({severity}) {key}: {summary} — {outcome}", "calling": "Anruf läuft",
           "no_call": "kein Anruf: {reason}"},
}

INFO_TEXTS = {
    "en": {
        "brief_gave_up": "The morning brief could not be sent today (3 attempts failed). Run personal-agent brief --send to retry.",
        "safety_stop": "The Telegram assistant was stopped by a safety stop. Check the logs, then run personal-agent safety clear in a terminal and restart serve.",
        "telegram_rejected": "The Telegram bot token was rejected: Telegram notices are off until the token is fixed and serve restarts.",
        "telegram_hijack": "Another client receives this bot's updates: the token may be leaked; revoke it with the bot owner tools and set a new PA_TELEGRAM_TOKEN. Telegram is off until serve restarts.",
    },
    "de": {
        "brief_gave_up": "Der Morgen-Brief konnte heute nicht gesendet werden (3 Versuche fehlgeschlagen). Erneut senden: personal-agent brief --send",
        "safety_stop": "Der Telegram-Assistent wurde durch einen Sicherheitsstopp angehalten. Logs prüfen, dann im Terminal personal-agent safety clear ausführen und serve neu starten.",
        "telegram_rejected": "Das Telegram-Bot-Token wurde abgelehnt: Telegram-Meldungen sind aus, bis das Token korrigiert und serve neu gestartet ist.",
        "telegram_hijack": "Ein anderer Client empfängt die Nachrichten dieses Bots: das Token ist womöglich bekannt geworden. Token mit den Bot-Verwaltungswerkzeugen widerrufen und ein neues PA_TELEGRAM_TOKEN setzen. Telegram ist aus, bis serve neu startet.",
    },
}
INFO_CODES = tuple(INFO_TEXTS["en"])


def _lang(table: dict, language: str) -> dict:
    return table.get(language, table["en"])


def render_approval_notice(target_name: str, tool_name: str, level: int, origin_label: str, context: bool,
                           minutes: int, link: str | None, language: str = "en", *,
                           sequence: int | None = None) -> str:
    """The approval notice. By construction it can only show the tool name, the level, the
    trusted origin label, a per-serve sequence number and the link: there is no parameter
    for the tool input."""
    t = _lang(NOTICE_TEXTS, language)
    if target_name == "discord":
        if not link:
            raise ValueError("a Discord approval notice needs the one-time link")
        return t["discord"].format(level=int(level), tool=discord_safe(tool_name, 60), link=link, minutes=minutes)
    label = LEVEL_LABELS.get(Level(int(level)), str(level)) if int(level) in (1, 2, 3) else str(int(level))
    origin = plain_line(origin_label, 80)
    head = t["head_seq"].format(seq=int(sequence), origin=origin) if sequence is not None else \
        t["head"].format(origin=origin)
    lines = [
        head,
        t["level"].format(level=int(level), label=label, tool=plain_line(tool_name, 60)),
    ]
    if context:
        lines.append(t["context"])
    lines.append(t["expires"].format(minutes=minutes))
    if link:
        lines.append(t["review"].format(link=link))
    return "\n".join(lines)


def alert_outcome(called: bool, reason: str, language: str = "en") -> str:
    """'calling you now' or 'no call: <policy reason>' (reasons are fixed policy strings)."""
    t = _lang(ALERT_TEXTS, language)
    return t["calling"] if called else t["no_call"].format(reason=clean_text(reason, 80))


def alert_notice(event: "Event", outcome: str, language: str = "en") -> Callable[[Target], str]:
    """An escalation event: severity (trusted), key and summary (untrusted, inert), outcome."""
    t = _lang(ALERT_TEXTS, language)
    severity = event.severity if event.severity in ("info", "warning", "critical") else "info"

    def build(target: Target) -> str:
        return t["alert"].format(severity=severity, key=target.inert(event.key, 80),
                                 summary=target.inert(event.summary, 160), outcome=target.inert(outcome, 120))

    return build


def task_counts_notice(done: int, failed: int, cancelled: int, running: int, queued: int) -> Callable[[Target | None], str]:
    """Task counts only: no ids, titles, domains, errors or paths."""
    text = (f"Tasks: {int(done)} done, {int(failed)} failed, {int(cancelled)} cancelled"
            f" · {int(running)} running, {int(queued)} queued")

    def build(target: Target | None) -> str:
        return text

    return build


def task_detail_notice(task_id: str, status: str, title: str | None) -> Callable[[Target], str]:
    """One task line for PA_NOTIFY_TASK_DETAIL=title: id, status and the cleaned title (<= 60).
    Never the result, the error, the domain or a path."""
    shown_id = task_id if isinstance(task_id, str) and _TASK_ID.fullmatch(task_id) else "-"
    shown_status = status if status in _TASK_STATES else "-"

    def build(target: Target) -> str:
        line = f"- task {shown_id} {shown_status}"
        if title:
            line += f": {target.inert(title, 60)}"
        return line

    return build


def info_notice(code: str, language: str = "en") -> Callable[[Target], str]:
    """A fixed operational notice; no variable text at all."""
    t = _lang(INFO_TEXTS, language)
    if code not in t:
        raise ValueError(f"unknown info notice: {code}")
    text = t[code]

    def build(target: Target) -> str:
        return text

    return build


def spend_notice(percent: int, day_usd: float, cap_day: float, month_usd: float, cap_month: float,
                 language: str = "en") -> Callable[[Target | None], str]:
    """API spend against the caps (access mode "api"): numbers only, no prompt text."""
    if language == "de":
        text = (f"API-Kosten bei {int(percent)} % des Monatsdeckels: heute ${day_usd:.2f} von ${cap_day:.2f}, "
                f"Monat ${month_usd:.2f} von ${cap_month:.2f}")
    else:
        text = (f"API spend at {int(percent)} % of the monthly cap: today ${day_usd:.2f} of ${cap_day:.2f}, "
                f"this month ${month_usd:.2f} of ${cap_month:.2f}")

    def build(target: Target | None) -> str:
        return text

    return build


def rhythm_notice(title: str, lines: list[str]) -> Callable[[Target], str]:
    """A midday, evening or weekly update: our own counts; any name in it is made inert."""
    def build(target: Target) -> str:
        inert = getattr(target, "inert", None)
        shown = [inert(line, 300) if inert else line[:300] for line in lines]
        return "\n".join([title, *shown])

    return build


NOTICE_FACTORIES = ("alert_notice", "info_notice", "task_counts_notice", "task_detail_notice", "spend_notice",
                    "rhythm_notice")


# --- routes --------------------------------------------------------------------


def resolve_route(config: "Config", kind: str, telegram_ready: bool) -> tuple[str, ...]:
    """The channel names for a kind. An explicit PA_NOTIFY_<KIND> is returned as configured
    (no fallback when a channel is not ready); otherwise the default rule applies."""
    if kind not in KINDS:
        raise ValueError(f"unknown notice kind: {kind}")
    explicit = config.route(kind)
    if explicit is not None:
        return () if explicit == ("none",) else explicit
    if telegram_ready:
        return ("telegram",)
    if kind == "brief":
        return ("discord",) if config.discord_webhook else ()
    if kind == "approval":
        return ("discord",) if config.discord_webhook and config.public_approval_url else ()
    if kind == "task_done":
        return ("discord",) if config.task_discord == "counts" and config.discord_webhook else ()
    return ()  # alert, info: opt-in on Discord


# --- the notifier --------------------------------------------------------------


class Notifier:
    """Sends notices of one kind to the channels of that kind.

    `send` is asynchronous (alert, info, task_done): a bounded queue and one sender
    thread, started on first use. `send_now` is synchronous and returns whether every
    resolved channel accepted every chunk.
    """

    def __init__(self, config: "Config", targets: dict[str, Target] | None = None, *,
                 clock: Callable[[], float] = time.monotonic, run_thread: bool = True,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.config = config
        self.targets: dict[str, Target] = dict(targets or {})
        self.clock = clock
        self.run_thread = run_thread
        self.sleep = sleep
        self._lock = threading.Lock()
        self._sent: dict[str, list[float]] = {}
        self._keys: dict[tuple[str, str], float] = {}
        self._reasons: dict[str, float] = {}
        self._queue: queue.Queue = queue.Queue(maxsize=QUEUE_SIZE)
        self._thread: threading.Thread | None = None
        self._closed = False
        self._overflow_logged = False

    def __repr__(self) -> str:
        return f"Notifier({', '.join(sorted(self.targets)) or 'no channels'})"

    @classmethod
    def from_config(cls, config: "Config", telegram: Target | None = None, **kwargs: object) -> "Notifier":
        """Discord when PA_DISCORD_WEBHOOK is set; Telegram when PA_TELEGRAM_TOKEN is set (or a
        Telegram channel is given). Telegram is ready only once the bot is paired."""
        targets: dict[str, Target] = {}
        if config.discord_webhook:
            from .remote import DiscordWebhook  # remote imports this module

            targets["discord"] = DiscordTarget(DiscordWebhook(config.discord_webhook))
        if telegram is None and config.telegram_token:
            from .telegram import TelegramTarget  # telegram imports this module

            telegram = TelegramTarget.from_config(config)
        if telegram is not None:
            targets["telegram"] = telegram
        return cls(config, targets, **kwargs)  # type: ignore[arg-type]

    # --- resolution ---

    def telegram_ready(self) -> bool:
        target = self.targets.get("telegram")
        return target is not None and target.ready() is None

    def _resolve(self, kind: str, exclude: tuple[str, ...] = ()) -> tuple[list[Target], list[str]]:
        ready: list[Target] = []
        reasons: list[str] = []
        for name in resolve_route(self.config, kind, self.telegram_ready()):
            if name in exclude:
                continue
            target = self.targets.get(name)
            reason = f"{name} channel not available" if target is None else target.ready()
            if reason is None:
                ready.append(target)  # type: ignore[arg-type]
            else:
                reasons.append(reason)
        return ready, reasons

    def targets_for(self, kind: str, exclude: tuple[str, ...] = ()) -> list[Target]:
        """The ready channels of a kind; each not-ready reason is logged at most every 10 min.
        exclude drops channels (a notice about Telegram itself never goes to Telegram)."""
        ready, reasons = self._resolve(kind, exclude)
        for reason in reasons:
            self._log_reason(kind, reason)
        return ready

    def unready_reasons(self, kind: str) -> list[str]:
        return self._resolve(kind)[1]

    def _log_reason(self, kind: str, reason: str) -> None:
        now = self.clock()
        with self._lock:
            last = self._reasons.get(reason)
            if last is not None and now - last < REASON_LOG_EVERY_S:
                return
            self._reasons[reason] = now
        log.warning("notify %s: skipped (%s)", kind, reason)

    # --- limits ---

    def _allow(self, kind: str, key: str | None) -> bool:
        """Alert and info: at most 10 per 10 min, and the same key at most once per hour."""
        if kind not in ("alert", "info"):
            return True
        now = self.clock()
        with self._lock:
            recent = [t for t in self._sent.get(kind, []) if now - t < RATE_WINDOW_S]
            self._sent[kind] = recent
            if key is not None:
                last = self._keys.get((kind, key))
                if last is not None and now - last < KEY_WINDOW_S:
                    return False
            if len(recent) >= RATE_LIMIT:
                log.warning("notify %s: dropped (rate limit)", kind)
                return False
            recent.append(now)
            if key is not None:
                self._keys[(kind, key)] = now
        return True

    # --- delivery ---

    def _deliver(self, kind: str, build: Callable[[Target], str], targets: list[Target]) -> bool:
        ok = True
        for target in targets:
            try:
                text = build(target)
            except Exception as exc:  # noqa: BLE001 - a broken notice never stops the caller
                log.warning("notify %s via %s: render failed (%s)", kind, target.name, type(exc).__name__)
                ok = False
                continue
            chunks = split_message(text, target.limit, target.max_chunks, target.measure, OVERFLOW_NOTE)
            sent = 0
            for index, chunk in enumerate(chunks):
                if index:
                    self.sleep(CHUNK_GAP_S)
                if not target.post(chunk):
                    break
                sent += 1
            if sent == len(chunks):
                log.info("notify %s via %s: sent", kind, target.name)
            else:
                log.warning("notify %s via %s: failed", kind, target.name)
                ok = False
        return ok

    def send_now(self, kind: str, build: Callable[[Target], str], *, key: str | None = None,
                 exclude: tuple[str, ...] = ()) -> bool:
        """Deliver synchronously. True when every resolved channel took every chunk (or none resolved)."""
        targets = self.targets_for(kind, exclude)
        if not targets:
            return True
        if not self._allow(kind, key):
            return True
        return self._deliver(kind, build, targets)

    def send(self, kind: str, build: Callable[[Target], str], *, key: str | None = None,
             exclude: tuple[str, ...] = ()) -> None:
        """Queue a notice (alert, info, task_done) for the sender thread; never blocks on the network."""
        if kind not in ASYNC_KINDS:
            raise ValueError(f"{kind} notices are sent synchronously")
        if self._closed:
            log.warning("notify %s: dropped (closing)", kind)
            return
        if not self.targets_for(kind, exclude) or not self._allow(kind, key):
            return
        if not self.run_thread:
            self._deliver(kind, build, self.targets_for(kind, exclude))
            return
        self._ensure_thread()
        try:
            self._queue.put_nowait((kind, build, exclude))
            self._overflow_logged = False
        except queue.Full:
            if not self._overflow_logged:
                self._overflow_logged = True
                log.warning("notify %s: dropped (queue full)", kind)

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop, name="notify-sender", daemon=True)
                self._thread.start()

    def _loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            kind, build, exclude = item
            try:
                self._deliver(kind, build, self.targets_for(kind, exclude))
            except Exception as exc:  # noqa: BLE001 - keep sending later notices
                log.warning("notify %s: failed (%s)", kind, type(exc).__name__)

    def close(self, timeout_s: float = 5.0) -> None:
        """Send what is queued (within timeout_s) and stop the sender thread."""
        self._closed = True
        with self._lock:
            thread = self._thread
        if thread is None:
            return
        try:
            self._queue.put(None, timeout=timeout_s)
        except queue.Full:
            log.warning("notify: queue still full at shutdown")
            return
        thread.join(timeout_s)


class NotifierPost:
    """Webhook-shaped adapter (`.post(text) -> bool`) for one kind, used by TaskNotifier.

    `post_build` takes a renderer, so untrusted fields are made inert per channel."""

    def __init__(self, notifier: Notifier, kind: str = "task_done") -> None:
        if kind not in KINDS:
            raise ValueError(f"unknown notice kind: {kind}")
        self.notifier = notifier
        self.kind = kind

    def __repr__(self) -> str:
        return f"NotifierPost({self.kind})"

    def post_build(self, build: Callable[[Target], str]) -> bool:
        return self.notifier.send_now(self.kind, build)

    def post(self, text: str) -> bool:
        return self.notifier.send_now(self.kind, lambda target: text)


__all__ = [
    "ASYNC_KINDS", "DISCORD_LIMIT", "DISCORD_MAX_CHUNKS", "KINDS", "TARGET_NAMES", "TELEGRAM_LIMIT",
    "TELEGRAM_MAX_CHUNKS", "DiscordTarget", "Notifier", "NotifierPost", "Target", "alert_notice", "alert_outcome",
    "channel_limits", "defang", "discord_line", "discord_safe", "info_notice", "plain_line", "render_approval_notice",
    "resolve_route", "split_message", "task_counts_notice", "task_detail_notice", "telegram_untrusted", "utf16_len",
]
