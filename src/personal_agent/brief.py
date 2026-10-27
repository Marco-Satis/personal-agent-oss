"""Morning brief: a deterministic daily summary, written to the vault and sent to the brief channels.

The brief is built without any model: date, today's focus from heute.md, open
"- [ ]" tasks from heute.md and wiki/ (raw/ is unprocessed capture and stays
out), a summary of the Claude Code session status files, the quota status and
the number of new captures. It is written to outputs/brief-YYYY-MM-DD.md.

The channels come from the notification layer (notify.py, PA_NOTIFY_BRIEF; by
default a paired Telegram bot, else PA_DISCORD_WEBHOOK). Discord gets far less by
default (PA_BRIEF_DISCORD=counts): counts, quota and session state counts only;
the full text stays in the vault. With "full", every string that comes from the
vault or a status file is escaped (no markup, no mentions) and URLs are
defanged, because notes hold text captured from mail and the web. Telegram gets
plain text (PA_BRIEF_TELEGRAM, default "full"), with the same strings made inert.

`--polish` lets the small model rewrite the brief text, at most once per day,
with no tools, no plugins and no session transcript, and only when the quota
guard allows it. Output that adds a URL, a domain, an "@" or a number that the
deterministic brief does not contain is rejected.

`serve` sends the brief once per day at PA_BRIEF_TIME. State lives in
state_dir/brief.json and every run holds an flock on state_dir/brief.lock, so a
manual send, a restart or a crash never causes a second post the same day, and
downtime past the grace window never causes a catch-up post. The exact chunks
of an attempt are kept per channel in state_dir/brief-chunks.json, so a retry
after a partial send posts only the remaining chunks of the channels that did not
get everything, even when the live data (quota, session status, captures) changed
in between. The polished text is kept in
state_dir/brief-polished.json; the copy in the vault is output only and never
read back, because the assistant can write the vault.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import logging
import math
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import date, datetime
from datetime import time as day_time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .bridge import BridgeConfig, BridgeError, ClaudeSession
from .plugins import prepare_state_dir
from .quota import QuotaGuard
from .spend import make_guard
from .notify import DISCORD_MARKUP as _DISCORD_MARKUP
from .notify import (
    TARGET_NAMES,
    DiscordTarget,
    Notifier,
    Target,
    channel_limits,
    defang,
    discord_line,
    info_notice,
    plain_line,
    split_message,
)
from .remote import DiscordWebhook
from .router import SafetyStop, guarded_send
from .statefiles import read_private_json, remove_private, write_private_json
from .status import STATES, SessionStatus, clean_text, format_status_line, read_statuses
from .textsafe import strip_invisible
from .vault import Task, Vault, VaultError

if TYPE_CHECKING:
    from .config import Config

log = logging.getLogger(__name__)

STATE_FILE = "brief.json"
LOCK_FILE = "brief.lock"
CHUNKS_FILE = "brief-chunks.json"      # the exact chunks per channel of today's attempt (resume source)
POLISHED_FILE = "brief-polished.json"  # today's polished text (outside the assistant-writable vault)
MAX_CHUNKS_BYTES = 256 * 1024
MAX_POLISHED_BYTES = 64 * 1024
STATE_VERSION = 2
POLISHED_VERSION = 1
MAX_STATE_BYTES = 8192
MAX_TASKS = 15
MAX_STATUS_LINES = 5
DISCORD_LIMIT = 1900
MAX_CHUNKS = 5
POST_GAP_S = 0.6
MAX_ATTEMPTS = 3
RETRY_AFTER_S = 300
LOCK_WAIT_S = 30.0
DEFAULT_SINCE_S = 24 * 3600
CAPTURE_WINDOW_S = 7 * 24 * 3600
POLISH_MAX_CHARS = 6000
POLISH_INSTRUCTION = (
    "Rewrite the brief below for readability. Keep every fact, number, date, task text and path. "
    "Add nothing. Output only the brief in Markdown."
)

WEEKDAYS = {
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
    "de": ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"),
}
TEXTS = {
    "en": {
        "title": "Brief {date} ({weekday})",
        "focus": "Focus (heute.md)",
        "tasks": "Open tasks",
        "sessions": "Claude Code sessions",
        "quota": "Quota",
        "inbox": "Inbox",
        "empty": "(empty)",
        "none": "(none)",
        "no_vault": "(no vault)",
        "more": "+{n} more",
        "no_status": "no session status",
        "captures": "{n} new captures since {since}",
        "lines": "{n} lines",
        "open": "{n} open",
        "full": "Full brief: {path}",
    },
    "de": {
        "title": "Brief {date} ({weekday})",
        "focus": "Fokus (heute.md)",
        "tasks": "Offene Aufgaben",
        "sessions": "Claude-Code-Sessions",
        "quota": "Kontingent",
        "inbox": "Eingang",
        "empty": "(leer)",
        "none": "(keine)",
        "no_vault": "(kein Vault)",
        "more": "+{n} weitere",
        "no_status": "kein Session-Status",
        "captures": "{n} neue Notizen seit {since}",
        "lines": "{n} Zeilen",
        "open": "{n} offen",
        "full": "Ganzer Brief: {path}",
    },
}

_STATUSES = ("sending", "sent", "failed")
_SENDERS = ("schedule", "manual")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_URL = re.compile(r"[a-z][a-z0-9+.-]*://|\bwww\.", re.IGNORECASE)
_DOMAIN = re.compile(r"\b[\w-]+\.[a-z]{2,}\b", re.IGNORECASE)
_NUMBER = re.compile(r"\d+")
_SPACES = re.compile(r"[ \t]+")
_HEADING = re.compile(r"^#{1,6} ")
_LOCK_FLAGS = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC

DEFAULT_STATE: dict = {
    "version": STATE_VERSION,
    "date": None,             # day of the last send attempt, YYYY-MM-DD
    "status": None,           # sending | sent | failed (for "date")
    "attempts": 0,            # failed sends on "date"
    "last_attempt_at": None,  # epoch of the last send attempt
    "last_brief_at": None,    # epoch of the last successful send or write: "since" of the next brief
    "sent_by": None,          # schedule | manual
    "polish_date": None,      # day for which the small model was already asked
    "since": None,            # "since" of the last send attempt, reused by a retry on the same day
    # Per channel of the last attempt: {"chunks_sent": n, "digest": sha256 of its chunk list,
    # "done": bool}. A retry resumes each channel that is not done after chunks_sent.
    "targets": {},
}
NO_CHANNEL = "[error] --send needs a brief channel (a paired Telegram bot or PA_DISCORD_WEBHOOK)"


class BriefError(RuntimeError):
    pass


# --- content ---------------------------------------------------------------------


@dataclass(frozen=True)
class BriefData:
    day: date
    weekday: str
    now_ts: float
    has_vault: bool
    focus: str
    tasks: tuple[Task, ...]
    task_total: int
    status_counts: tuple[tuple[str, int], ...]
    statuses: tuple[SessionStatus, ...]
    quota: str
    captures: int
    since_text: str


def _texts(language: str) -> dict[str, str]:
    return TEXTS.get(language, TEXTS["en"])


def collect_brief(vault: Vault | None, guard: QuotaGuard, statuses: list[SessionStatus],
                  now: datetime, since: float, language: str = "en") -> BriefData:
    """Everything the brief shows, read once. No model, no network."""
    now_ts = now.timestamp()
    has_vault = vault is not None and vault.root.is_dir()
    focus, tasks, total, captures = "", [], 0, 0
    since = max(since, now_ts - CAPTURE_WINDOW_S)
    if has_vault:
        assert vault is not None
        focus = vault.focus_text()
        tasks, total = vault.open_tasks(MAX_TASKS, include_raw=False)
        captures = vault.count_captures_since(since, now=now_ts)
    shown_since = datetime.fromtimestamp(since)
    since_text = shown_since.strftime("%H:%M" if shown_since.date() == now.date() else "%Y-%m-%d %H:%M")
    counts = tuple((state, n) for state in STATES if (n := sum(1 for s in statuses if s.state == state)))
    weekdays = WEEKDAYS.get(language, WEEKDAYS["en"])
    return BriefData(
        day=now.date(),
        weekday=weekdays[now.weekday()],
        now_ts=now_ts,
        has_vault=has_vault,
        focus=focus,
        tasks=tuple(tasks),
        task_total=total,
        status_counts=counts,
        statuses=tuple(statuses[:MAX_STATUS_LINES]),
        quota=guard.describe(),
        captures=captures,
        since_text=since_text,
    )


def _counts_text(data: BriefData) -> str:
    return ", ".join(f"{n} {state}" for state, n in data.status_counts)


def render_markdown(data: BriefData, language: str = "en") -> str:
    """The brief as written to outputs/brief-YYYY-MM-DD.md."""
    t = _texts(language)
    lines = [f"# {t['title'].format(date=data.day.isoformat(), weekday=data.weekday)}", "", f"## {t['focus']}"]
    if not data.has_vault or not data.focus:
        lines.append(t["no_vault"] if not data.has_vault else t["empty"])
    else:
        # Headings of heute.md become sub-headings, so they cannot end the brief's sections.
        lines += [_HEADING.sub("### ", line) for line in data.focus.splitlines()]
    lines += ["", f"## {t['tasks']}"]
    if not data.has_vault:
        lines.append(t["no_vault"])
    elif not data.tasks:
        lines.append(t["none"])
    else:
        lines += [f"- [ ] {task.text} — {task.path}" for task in data.tasks]
        if data.task_total > len(data.tasks):
            lines.append(t["more"].format(n=data.task_total - len(data.tasks)))
    lines += ["", f"## {t['sessions']}"]
    if not data.status_counts:
        lines.append(t["no_status"])
    else:
        lines.append(_counts_text(data))
        lines += [f"- {format_status_line(s, data.now_ts).rstrip()}" for s in data.statuses]
    lines += ["", f"## {t['quota']}", data.quota, "", f"## {t['inbox']}"]
    if data.has_vault:
        lines.append(t["captures"].format(n=data.captures, since=data.since_text))
    else:
        lines.append(t["no_vault"])
    return "\n".join(lines) + "\n"


def build_brief(vault: Vault | None, guard: QuotaGuard, statuses: list[SessionStatus],
                now: datetime, since: float, language: str = "en") -> str:
    """The deterministic brief: the same input gives byte-identical output."""
    return render_markdown(collect_brief(vault, guard, statuses, now, since, language), language)


# --- channels: Discord and plain text ----------------------------------------------
# defang and discord_line live in notify.py (shared with every other notice).


def _discord_plain(text: str, limit: int = 400) -> str:
    """Our own text (quota line): markup escaped, URLs defanged, kept otherwise."""
    flat = _SPACES.sub(" ", strip_invisible(text.replace("\n", " "))).strip()[:limit]
    return defang(_DISCORD_MARKUP.sub(r"\\\1", flat))


def render_discord(data: BriefData, language: str, mode: str, full_path: str,
                   polished: str | None = None) -> str:
    """The Discord message. "counts" leaves every note text out; "full" escapes it."""
    t = _texts(language)
    title = t["title"].format(date=data.day.isoformat(), weekday=data.weekday)
    footer = t["full"].format(path=full_path)
    if mode != "full":
        if not data.has_vault:
            focus = tasks = inbox = t["no_vault"]
        else:
            focus = t["lines"].format(n=data.focus.count("\n") + 1) if data.focus else t["empty"]
            tasks = t["open"].format(n=data.task_total)
            inbox = t["captures"].format(n=data.captures, since=data.since_text)
        return "\n".join([
            f"**{title}**",
            f"{t['focus']}: {focus}",
            f"{t['tasks']}: {tasks}",
            f"{t['sessions']}: {_counts_text(data) or t['no_status']}",
            f"{t['quota']}: {_discord_plain(data.quota)}",
            f"{t['inbox']}: {inbox}",
            footer,
        ])
    if polished is not None:
        body = [discord_line(line, 400) for line in polished.splitlines() if line.strip()]
        return "\n".join([f"**{title}**", *body, footer])
    lines = [f"**{title}**", "", f"**{t['focus']}**"]
    if not data.has_vault:
        lines.append(t["no_vault"])
    elif not data.focus:
        lines.append(t["empty"])
    else:
        lines += [discord_line(line, 200) for line in data.focus.splitlines()]
    lines += ["", f"**{t['tasks']}**"]
    if not data.has_vault:
        lines.append(t["no_vault"])
    elif not data.tasks:
        lines.append(t["none"])
    else:
        lines += [f"- {discord_line(task.text, 160)} — {discord_line(task.path, 80)}" for task in data.tasks]
        if data.task_total > len(data.tasks):
            lines.append(t["more"].format(n=data.task_total - len(data.tasks)))
    lines += ["", f"**{t['sessions']}**"]
    if not data.status_counts:
        lines.append(t["no_status"])
    else:
        lines.append(_counts_text(data))
        # No minutes here: a retry must produce the same chunks to resume after a partial send.
        lines += [
            f"- {s.state} · {discord_line(s.project, 48)}" + (f" · {discord_line(s.summary, 120)}" if s.summary else "")
            for s in data.statuses
        ]
    lines += ["", f"**{t['quota']}**", _discord_plain(data.quota), "", f"**{t['inbox']}**"]
    lines.append(t["captures"].format(n=data.captures, since=data.since_text) if data.has_vault else t["no_vault"])
    lines.append(footer)
    return "\n".join(lines)


def _plain(text: str, limit: int = 400) -> str:
    """Our own text (quota line) as one plain line, URLs defanged."""
    return defang(_SPACES.sub(" ", strip_invisible(text.replace("\n", " "))).strip()[:limit])


def render_plain(data: BriefData, language: str, mode: str, full_path: str, polished: str | None = None,
                 inert: Callable[[object, int], str] = plain_line) -> str:
    """The brief as plain text (Telegram: no markup at all). The same sections and the same
    privacy modes as render_discord; every vault- or status-derived string goes through inert.

    Sessions are counts only, also in "full" mode: project names and waiting texts of the
    Claude Code sessions stay on this machine (the same rule as `status` and `sessions`
    over Telegram, see router.LOCAL_ONLY)."""
    t = _texts(language)
    title = t["title"].format(date=data.day.isoformat(), weekday=data.weekday)
    footer = t["full"].format(path=full_path)
    if mode != "full":
        if not data.has_vault:
            focus = tasks = inbox = t["no_vault"]
        else:
            focus = t["lines"].format(n=data.focus.count("\n") + 1) if data.focus else t["empty"]
            tasks = t["open"].format(n=data.task_total)
            inbox = t["captures"].format(n=data.captures, since=data.since_text)
        return "\n".join([
            title,
            f"{t['focus']}: {focus}",
            f"{t['tasks']}: {tasks}",
            f"{t['sessions']}: {_counts_text(data) or t['no_status']}",
            f"{t['quota']}: {_plain(data.quota)}",
            f"{t['inbox']}: {inbox}",
            footer,
        ])
    if polished is not None:
        body = [inert(line, 400) for line in polished.splitlines() if line.strip()]
        return "\n".join([title, *body, footer])
    lines = [title, "", t["focus"]]
    if not data.has_vault:
        lines.append(t["no_vault"])
    elif not data.focus:
        lines.append(t["empty"])
    else:
        lines += [inert(line, 200) for line in data.focus.splitlines()]
    lines += ["", t["tasks"]]
    if not data.has_vault:
        lines.append(t["no_vault"])
    elif not data.tasks:
        lines.append(t["none"])
    else:
        lines += [f"- {inert(task.text, 160)} — {inert(task.path, 80)}" for task in data.tasks]
        if data.task_total > len(data.tasks):
            lines.append(t["more"].format(n=data.task_total - len(data.tasks)))
    lines += ["", t["sessions"], _counts_text(data) or t["no_status"]]
    lines += ["", t["quota"], _plain(data.quota), "", t["inbox"]]
    lines.append(t["captures"].format(n=data.captures, since=data.since_text) if data.has_vault else t["no_vault"])
    lines.append(footer)
    return "\n".join(lines)


def mode_for(config: "Config", target_name: str) -> str:
    """What the brief shows on a channel: PA_BRIEF_DISCORD or PA_BRIEF_TELEGRAM."""
    return config.brief_discord if target_name == "discord" else config.brief_telegram


def render_for_target(target: Target, data: BriefData, language: str, mode: str, full_path: str,
                      polished: str | None = None) -> str:
    if target.name == "discord":
        return render_discord(data, language, mode, full_path, polished)
    return render_plain(data, language, mode, full_path, polished, inert=target.inert)


def _truncation_note(full_path: str) -> str:
    return f"\n… (truncated; full brief in {full_path})"


def discord_chunks(text: str, limit: int = DISCORD_LIMIT, max_chunks: int = MAX_CHUNKS,
                   full_path: str = "outputs/") -> list[str]:
    """Split at line boundaries into messages of at most limit characters.

    A single longer line is hard-split. Beyond max_chunks the last chunk ends with a
    truncation note. Control and invisible characters except newlines are removed.
    """
    return split_message(text, limit, max_chunks, len, _truncation_note(full_path))


def target_chunks(target: Target, text: str, full_path: str = "outputs/") -> list[str]:
    """The brief split for one channel (its own limit, chunk count and unit)."""
    return split_message(text, target.limit, target.max_chunks, target.measure, _truncation_note(full_path))


def _post_chunks(target: Target, chunks: list[str], start: int, sleep: Callable[[float], None]) -> int:
    """Post chunks[start:] in order; returns how many chunks are posted in total."""
    total = len(chunks)
    for index in range(start, total):
        if index > start:
            sleep(POST_GAP_S)
        if not target.post(chunks[index]):
            log.warning("brief send to %s failed at chunk %d/%d", target.name, index + 1, total)
            return index
    log.info("brief sent to %s in %d chunks", target.name, total - start)
    return total


def send_brief(webhook: DiscordWebhook, text: str, sleep: Callable[[float], None] = time.sleep) -> bool:
    """Post the brief to a Discord webhook in chunks; stops at the first failure and returns False."""
    chunks = discord_chunks(text)
    return _post_chunks(DiscordTarget(webhook), chunks, 0, sleep) == len(chunks)


# --- polish ------------------------------------------------------------------------


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def polish_prompt(text: str) -> str:
    return f"{POLISH_INSTRUCTION}\n\n<brief>\n{_escape(text)}\n</brief>"


def _number(token: str) -> str:
    return token.lstrip("0") or "0"


def validate_polish(polished: str, source: str) -> str | None:
    """The cleaned polished text, or None when it may have added anything.

    Rejected: empty, too long, any URL or "@", a domain-like token or a number that
    the deterministic brief does not contain.
    """
    text = strip_invisible(polished, keep="\n").strip()
    if not text or len(text) > max(2 * len(source), 2000) or len(text) > POLISH_MAX_CHARS:
        return None
    if _URL.search(text) or "@" in text:
        return None
    domains = {token.lower() for token in _DOMAIN.findall(source)}
    if any(token.lower() not in domains for token in _DOMAIN.findall(text)):
        return None
    numbers = {_number(token) for token in _NUMBER.findall(source)}
    if any(_number(token) not in numbers for token in _NUMBER.findall(text)):
        return None
    return text


def polish_brief(text: str, session: ClaudeSession, guard: QuotaGuard) -> tuple[str | None, str | None]:
    """(polished, refusal_reason). The caller closes the session afterwards."""
    check = guard.check()
    if not check.ok:
        return None, check.reason
    try:
        result = guarded_send(session, polish_prompt(text), guard)
    except SafetyStop as exc:
        session.close(timeout_s=0)
        return None, clean_text(str(exc), 200)
    except BridgeError as exc:
        return None, f"the small model failed ({type(exc).__name__})"
    if result.is_error:
        return None, "polish output rejected"
    polished = validate_polish(result.text, text)
    if polished is None:
        return None, "polish output rejected"
    return polished, None


def _polish_session(config: "Config") -> ClaudeSession:
    """Small model, no tools, no MCP, no plugins, no transcript, in a fresh private dir.

    The working directory is not the vault, so the vault's CLAUDE.md is never loaded.
    """
    assert config.router_small_model
    run_dir = config.state_dir / "run"
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="polish-", dir=run_dir))
    _, config_dir = config.bridge_paths()
    return ClaudeSession(
        BridgeConfig(
            workdir=workdir,
            config_dir=config_dir,
            claude_bin=config.claude_bin,
            model=config.router_small_model,
            tools=(),
            persist_session=False,
            oauth_token=config.claude_oauth_token,
            api_key=config.claude_api_key,
        )
    )


# --- state and lock ------------------------------------------------------------------


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10_000


_TOP_CHECKS = {
    "date": lambda v: isinstance(v, str) and _DATE.fullmatch(v) is not None,
    "status": lambda v: v in _STATUSES,
    "attempts": _is_count,
    "last_attempt_at": _is_number,
    "last_brief_at": _is_number,
    "sent_by": lambda v: v in _SENDERS,
    "polish_date": lambda v: isinstance(v, str) and _DATE.fullmatch(v) is not None,
    "since": _is_number,
}


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _valid_target_record(name: object, record: object) -> bool:
    return (
        name in TARGET_NAMES
        and isinstance(record, dict)
        and set(record) <= {"chunks_sent", "digest", "done"}
        and _is_count(record.get("chunks_sent", 0))
        and (record.get("digest") is None or _valid_digest(record["digest"]))
        and isinstance(record.get("done", False), bool)
    )


def _valid_state(data: object) -> bool:
    """A v1 (Discord only, flat chunk fields) or v2 (per channel) state."""
    if not isinstance(data, dict) or isinstance(data.get("version"), bool):
        return False
    if not all(data.get(key) is None or check(data[key]) for key, check in _TOP_CHECKS.items()):
        return False
    if data.get("version") == 1:
        return ((data.get("chunks_sent") is None or _is_count(data["chunks_sent"]))
                and (data.get("digest") is None or _valid_digest(data["digest"])))
    if data.get("version") != STATE_VERSION:
        return False
    targets = data.get("targets")
    return targets is None or (isinstance(targets, dict)
                               and all(_valid_target_record(k, v) for k, v in targets.items()))


def _clean_record(record: dict) -> dict:
    return {"chunks_sent": record.get("chunks_sent", 0), "digest": record.get("digest"),
            "done": bool(record.get("done", False))}


def _migrate(data: dict) -> dict:
    """The state as v2. A v1 attempt becomes the record of the Discord channel."""
    state = {key: value for key, value in DEFAULT_STATE.items() if key != "targets"}
    state.update({key: data[key] for key in _TOP_CHECKS if data.get(key) is not None})
    if data.get("version") == 1:
        digest = data.get("digest")
        state["targets"] = {} if digest is None else {
            "discord": {"chunks_sent": data.get("chunks_sent") or 0, "digest": digest,
                        "done": data.get("status") == "sent"}}
    else:
        state["targets"] = {name: _clean_record(record) for name, record in (data.get("targets") or {}).items()}
    state["version"] = STATE_VERSION
    return state


def fresh_state() -> dict:
    """An empty state with its own targets dict (DEFAULT_STATE is never mutated)."""
    state = dict(DEFAULT_STATE)
    state["targets"] = {}
    return state


def load_state(config: "Config") -> tuple[dict | None, bool]:
    """(state, corrupt). A missing file is (None, False); anything invalid is (None, True).

    A v1 file (from before per-channel state) is migrated in memory and written as v2
    on the next save.
    """
    path = config.state_dir / STATE_FILE
    try:
        fd = os.open(path, _READ_FLAGS)
    except FileNotFoundError:
        return None, False
    except OSError:
        return None, True
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_STATE_BYTES:
            return None, True
        raw = os.read(fd, MAX_STATE_BYTES + 1)
    except OSError:
        return None, True
    finally:
        os.close(fd)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None, True
    if len(raw) > MAX_STATE_BYTES or not _valid_state(data):
        return None, True
    return _migrate(data), False


def save_state(config: "Config", state: dict) -> bool:
    """Write brief.json (v2) atomically (unique temp file, 0600). False on any error."""
    payload = {key: state.get(key, DEFAULT_STATE[key]) for key in DEFAULT_STATE}
    payload["version"] = STATE_VERSION
    payload["targets"] = {name: _clean_record(record) for name, record in (state.get("targets") or {}).items()}
    return write_private_json(config.state_dir, STATE_FILE, payload, prefix=".brief-")


def _chunks_digest(chunks: list[str]) -> str:
    return hashlib.sha256("\0".join(chunks).encode("utf-8")).hexdigest()


def save_chunks(config: "Config", day: str, targets: dict[str, dict]) -> bool:
    """Store the exact chunks of today's attempt per channel: {name: {"mode", "chunks"}}."""
    return write_private_json(config.state_dir, CHUNKS_FILE, {
        "version": STATE_VERSION,
        "date": day,
        "targets": {name: {"mode": item["mode"], "digest": _chunks_digest(item["chunks"]), "chunks": item["chunks"]}
                    for name, item in targets.items()},
    }, prefix=".brief-")


def load_chunks(config: "Config", day: str, target_name: str, mode: str, digest: str | None) -> list[str] | None:
    """The stored chunks of one channel for today's attempt, only when date, mode and digest
    all match and every chunk fits that channel's limit."""
    data = read_private_json(config.state_dir, CHUNKS_FILE, MAX_CHUNKS_BYTES, (1, STATE_VERSION))
    if data is None or digest is None or data.get("date") != day or target_name not in TARGET_NAMES:
        return None
    if data.get("version") == 1:  # from before per-channel state: Discord only
        item = data if target_name == "discord" else None
    else:
        targets = data.get("targets")
        item = targets.get(target_name) if isinstance(targets, dict) else None
    if not isinstance(item, dict) or item.get("mode") != mode:
        return None
    limit, max_chunks, measure = channel_limits(target_name)
    chunks = item.get("chunks")
    if (not isinstance(chunks, list) or not chunks or len(chunks) > max_chunks
            or not all(isinstance(c, str) and 0 < measure(c) <= limit for c in chunks)):
        return None
    if item.get("digest") != digest or _chunks_digest(chunks) != digest:
        return None
    return chunks


def save_polished(config: "Config", day: str, text: str) -> bool:
    return write_private_json(config.state_dir, POLISHED_FILE,
                              {"version": POLISHED_VERSION, "date": day, "text": text}, prefix=".brief-")


def load_polished(config: "Config", day: str) -> str | None:
    data = read_private_json(config.state_dir, POLISHED_FILE, MAX_POLISHED_BYTES, POLISHED_VERSION)
    if data is None or data.get("date") != day:
        return None
    text = data.get("text")
    return text if isinstance(text, str) and 0 < len(text) <= POLISH_MAX_CHARS else None


def _open_lock(config: "Config") -> int:
    """A descriptor of state_dir/brief.lock, after checking that the state dir is ours."""
    problem = prepare_state_dir(config.state_dir)
    if problem:
        raise BriefError(f"the state directory {problem}")
    try:
        fd = os.open(config.state_dir / LOCK_FILE, _LOCK_FLAGS, 0o600)
    except OSError as exc:
        raise BriefError(f"cannot open the brief lock ({type(exc).__name__})") from exc
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise BriefError("the brief lock is not a regular file")
    return fd


def _try_lock(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return False
        raise
    return True


# --- one run -------------------------------------------------------------------------


def _stderr(line: str) -> None:
    print(line, file=sys.stderr)


def _write_output(vault: Vault, name: str, text: str, note: Callable[[str], None]) -> bool:
    try:
        rel = vault.write_output(name, text)
    except VaultError as exc:
        note(f"[warning] brief file not written: {exc}")
        return False
    log.debug("brief written to %s", json.dumps(rel))
    return True


def _polished(config: "Config", vault: Vault, text: str, today: str, state: dict, guard: QuotaGuard,
              session_factory: Callable[[], ClaudeSession] | None, note: Callable[[str], None]) -> str | None:
    """The polished brief for today: the saved one, or one new model call per day.

    The saved text comes from state_dir, never from the vault copy: the assistant can
    write outputs/ (after one approval), so injected note text could plant a file there.
    """
    name = f"brief-{today}.polished.md"
    saved = load_polished(config, today)
    if saved is not None:
        polished = validate_polish(saved, text)
        if polished is None:
            note("[polish] the saved polished brief does not match today's brief: using the plain brief")
        return polished
    if state.get("polish_date") == today:
        note("[polish] already tried today: using the plain brief")
        return None
    if not config.router_small_model:
        note("[quota] polish refused: PA_ROUTER_SMALL_MODEL is off")
        return None
    check = guard.check()
    if not check.ok:
        note(f"[quota] polish refused: {check.reason}")
        return None
    state["polish_date"] = today  # recorded before the call: retries never pay twice
    if not save_state(config, state):
        note("[polish] could not record the attempt: using the plain brief")
        return None
    session = session_factory() if session_factory is not None else _polish_session(config)
    try:
        polished, reason = polish_brief(text, session, guard)
    finally:
        session.close()
        if session_factory is None:
            shutil.rmtree(session.config.workdir, ignore_errors=True)
    if polished is None:
        note(f"[polish] not used: {reason}")
        return None
    if not save_polished(config, today, polished):
        note("[polish] could not keep the polished text for retries")
    _write_output(vault, name, polished, note)  # output only: never read back
    return polished


def _run_locked(config: "Config", now: datetime, send: bool, polish: bool, by: str,
                targets: list[Target] | None, session_factory: Callable[[], ClaudeSession] | None = None,
                out: Callable[[str], None] | None = None, note: Callable[[str], None] = _stderr,
                sleep: Callable[[float], None] = time.sleep) -> int:
    """Build, write, optionally polish and send to the given channels. The caller holds the lock.

    0 ok, 1 send failed (or its state could not be recorded), 2 configuration.
    """
    ts = now.timestamp()
    today = now.strftime("%Y-%m-%d")
    state, corrupt = load_state(config)
    if corrupt:
        log.warning("brief state unreadable: treated as empty")
    state = state or fresh_state()
    last = state["last_brief_at"]
    since = last if last is not None and last < ts else ts - DEFAULT_SINCE_S
    retry = send and state["date"] == today and state["status"] == "failed"
    if retry and state["since"] is not None and state["since"] < ts:
        since = state["since"]  # the same brief as the failed attempt, so the send can resume

    vault = Vault(config.vault_path, config.language)
    guard = make_guard(config)  # fresh: sees the chat's usage
    statuses = read_statuses(config.status_dir, config.status_max_age_min, now=ts)
    data = collect_brief(vault, guard, statuses, now, since, config.language)
    text = render_markdown(data, config.language)
    name = f"brief-{today}.md"
    written = _write_output(vault, name, text, note)
    log.info("brief built: %d open tasks, %d sessions, %d new captures", data.task_total, len(statuses), data.captures)

    # The polish input leaves the per-session lines out (counts stay): a polished text may be
    # posted to a third party, and session names and waiting texts stay on this machine.
    polish_source = render_markdown(replace(data, statuses=()), config.language)
    polished = _polished(config, vault, polish_source, today, state, guard, session_factory, note) if polish else None
    if out is not None:
        out(polished or text)
    if not send:
        if written:
            state["last_brief_at"] = ts
            save_state(config, state)
        return 0
    if not targets:
        note(NO_CHANNEL)
        return 2

    full_path = f"outputs/{name}"
    live = {target.name: target for target in targets if target.ready() is None}
    recorded = state["targets"] if retry else {}
    # A retry continues the channels recorded in the failed attempt that are not done; a
    # channel that became ready in between is not added mid-day. A fresh attempt uses
    # every given channel.
    if recorded:
        attempt = {n: dict(recorded[n]) for n in TARGET_NAMES if n in recorded}
    else:
        attempt = {t.name: {"chunks_sent": 0, "digest": None, "done": False} for t in targets}
    plans: list[tuple[str, Target | None, list[str] | None, int]] = []
    store: dict[str, dict] = {}
    rendered = False
    for target_name, record in attempt.items():
        if record["done"]:
            continue
        mode = mode_for(config, target_name)
        target = live.get(target_name)
        # A retry resends the stored chunks of the failed attempt, not a fresh rendering: the
        # live data (quota line, session status, captures) changes between attempts, and a new
        # rendering would post the chunks that already went out a second time.
        chunks = load_chunks(config, today, target_name, mode, record["digest"]) if recorded else None
        if chunks is not None:
            start = min(record["chunks_sent"], len(chunks))
        elif target is not None:
            chunks = target_chunks(target, render_for_target(target, data, config.language, mode, full_path,
                                                             polished), full_path)
            record.update(digest=_chunks_digest(chunks), chunks_sent=0)
            start = 0
            rendered = True
        else:
            start = 0
        if chunks is not None:
            store[target_name] = {"mode": mode, "chunks": chunks}
        plans.append((target_name, target, chunks, start))
    if rendered and not save_chunks(config, today, store):
        note("[warning] brief chunks not stored: a retry after a partial send would repost them")
    same_day = state["date"] == today
    attempts = state["attempts"] if same_day else 0
    state.update(date=today, status="sending", attempts=attempts, last_attempt_at=ts, sent_by=by,
                 since=since, targets=attempt)
    if not save_state(config, state):
        # Without a recorded "sending" a crash could mean a second post: do not post at all.
        note("[error] brief not sent: could not record the sending state")
        return 1
    several = len(attempt) > 1
    for target_name, target, chunks, start in plans:
        if target is None or chunks is None:
            note(f"[error] brief not sent to {target_name}: the channel is not ready")
            continue
        posted = _post_chunks(target, chunks, start, sleep)
        attempt[target_name]["chunks_sent"] = posted
        if posted == len(chunks):
            attempt[target_name]["done"] = True
        else:
            note(f"[error] brief send failed at chunk {posted + 1}/{len(chunks)}"
                 + (f" ({target_name})" if several else ""))
    if all(record["done"] for record in attempt.values()):
        state.update(status="sent", last_brief_at=ts)
        remove_private(config.state_dir, CHUNKS_FILE)  # sent: no resume needed, no copy of the text kept
        code = 0
    else:
        state.update(status="failed", attempts=attempts + 1)
        code = 1
    save_state(config, state)
    return code


def run_brief(config: "Config", send: bool, polish: bool, by: str = "manual",
              now: datetime | None = None, webhook: DiscordWebhook | None = None,
              session_factory: Callable[[], ClaudeSession] | None = None, *,
              targets: list[Target] | None = None,
              out: Callable[[str], None] | None = None, note: Callable[[str], None] = _stderr,
              lock_wait_s: float = LOCK_WAIT_S, sleep: Callable[[float], None] = time.sleep) -> int:
    """One brief run under the lock (waits up to lock_wait_s). 0 ok, 1 failed or busy, 2 configuration.

    The channels: `webhook` (one Discord webhook), else `targets`, else the ready brief
    channels of the notification layer (PA_NOTIFY_BRIEF or the default rule).
    """
    if send:
        if webhook is not None:
            targets = [DiscordTarget(webhook)]
        elif targets is None:
            targets = Notifier.from_config(config, run_thread=False).targets_for("brief")
        if not targets:
            note(NO_CHANNEL)
            return 2
    try:
        fd = _open_lock(config)
    except BriefError as exc:
        note(f"[error] {exc}")
        return 1
    try:
        deadline = time.monotonic() + lock_wait_s
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                note("[error] another brief is running")
                return 1
            time.sleep(0.2)
        return _run_locked(config, now or datetime.now(), send, polish, by, targets, session_factory,
                           out, note, sleep)
    finally:
        os.close(fd)  # also releases the flock


# --- scheduler -----------------------------------------------------------------------


def _log_note(line: str) -> None:
    log.warning("%s", line)


class BriefScheduler:
    """Decides on every tick whether today's brief is due; sends it at most once per day.

    State is re-read from brief.json under the lock on every tick, so manual sends
    and restarts count. In-memory counters per day (at most MAX_ATTEMPTS runs, at
    least RETRY_AFTER_S apart) make sure that a deleted, corrupt or unwritable
    brief.json can never cause a post on every tick.
    """

    def __init__(self, config: "Config", run: Callable[[datetime], bool] | None = None,
                 clock: Callable[[], float] = time.time, webhook: DiscordWebhook | None = None,
                 notifier: Notifier | None = None) -> None:
        self.config = config
        self.clock = clock
        self._webhook = webhook  # one fixed Discord webhook (tests); otherwise the notifier decides
        self._notifier = notifier
        self.run = run or self._send_now
        self._day: str | None = None
        self._attempts = 0
        self._last_attempt: float | None = None
        self._sent = False
        self._gave_up_noted = False
        self._logged: set[str] = set()

    def _messenger(self) -> Notifier:
        if self._notifier is None:
            self._notifier = Notifier.from_config(self.config, run_thread=False)
        return self._notifier

    def _targets(self) -> tuple[list[Target], list[str]]:
        """The channels for this run, resolved every time (a pairing made meanwhile counts)."""
        if self._webhook is not None:
            return [DiscordTarget(self._webhook)], []
        messenger = self._messenger()
        return messenger.targets_for("brief"), messenger.unready_reasons("brief")

    def _send_now(self, now: datetime) -> bool:
        """The default run; called with the lock held."""
        targets, reasons = self._targets()
        if not targets:
            self._once("channel", f"morning brief: no brief channel ready ({', '.join(reasons) or 'none configured'})")
            return False
        # The scheduled run prints nothing, so a polished text is only seen in "full" mode:
        # in "counts" mode a polish would spend a model call every day for a file nobody reads.
        polish = self.config.brief_polish and any(mode_for(self.config, t.name) == "full" for t in targets)
        if self.config.brief_polish and not polish:
            self._once("polish", "morning brief: PA_BRIEF_POLISH skipped (only used when a channel shows "
                                 "the full brief: PA_BRIEF_DISCORD=full or PA_BRIEF_TELEGRAM=full)")
        return _run_locked(self.config, now, send=True, polish=polish, by="schedule",
                           targets=targets, note=_log_note) == 0

    def _note_gave_up(self) -> None:
        """One info notice per day when the brief gives up (PA_NOTIFY_INFO; opt-in on Discord)."""
        if self._gave_up_noted:
            return
        self._gave_up_noted = True
        if self._webhook is not None and self._notifier is None:
            return  # fixed-webhook mode (tests): no notification layer
        try:
            self._messenger().send("info", info_notice("brief_gave_up", self.config.language), key="brief_gave_up")
        except Exception as exc:  # noqa: BLE001 - a notice never stops the scheduler
            log.warning("morning brief: info notice failed (%s)", type(exc).__name__)

    def scheduled_at(self, day: date) -> float:
        """Epoch of today's brief time (naive local time; DST is handled by the platform)."""
        assert self.config.brief_time is not None
        hour, minute = self.config.brief_time
        return datetime.combine(day, day_time(hour, minute)).timestamp()

    def _once(self, key: str, message: str) -> None:
        if key not in self._logged:
            self._logged.add(key)
            log.warning("%s", message)

    def tick(self) -> str:
        """off | not_due | missed | already | locked | sent | failed | gave_up."""
        if self.config.brief_time is None:
            return "off"
        now_ts = self.clock()
        now = datetime.fromtimestamp(now_ts)
        today = now.date()
        day = today.isoformat()
        if self._day != day:
            self._day, self._attempts, self._last_attempt, self._sent = day, 0, None, False
            self._gave_up_noted = False
            self._logged.clear()
        scheduled = self.scheduled_at(today)
        if now_ts < scheduled:
            return "not_due"
        try:
            fd = _open_lock(self.config)
        except BriefError as exc:
            self._once("lock", f"morning brief skipped: {exc}")
            return "failed"
        try:
            if not _try_lock(fd):
                return "locked"
            state, corrupt = load_state(self.config)
            ours = state is not None and state["date"] == day
            if now_ts >= scheduled + self.config.brief_grace_min * 60:
                if not ours:
                    self._once("missed", "brief for today missed (outside grace window)")
                return "missed"
            if self._sent:
                return "already"
            if ours:
                assert state is not None
                if state["status"] == "sent":
                    return "already"
                if state["status"] == "sending":
                    self._once("sending", "brief state 'sending' left by an interrupted run: today counts as sent")
                    return "already"
                if state["status"] == "failed":
                    if state["attempts"] >= MAX_ATTEMPTS:
                        self._note_gave_up()
                        return "gave_up"
                    if state["last_attempt_at"] is not None and now_ts - state["last_attempt_at"] < RETRY_AFTER_S:
                        return "not_due"
            if corrupt and self._attempts > 0:
                return "already"  # unknown state after our own attempt: fail toward not sending
            if self._attempts >= MAX_ATTEMPTS:
                self._once("gave_up", "morning brief: giving up for today after 3 attempts")
                self._note_gave_up()
                return "gave_up"
            if self._last_attempt is not None and now_ts - self._last_attempt < RETRY_AFTER_S:
                return "not_due"
            self._attempts += 1
            self._last_attempt = now_ts
            if self.run(now):
                self._sent = True
                return "sent"
            return "failed"
        finally:
            os.close(fd)
