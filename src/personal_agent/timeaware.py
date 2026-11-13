"""Time awareness: each model turn knows the time, the gap since the last message and what happened since.

The router puts one short line in front of every tier-2 and tier-3 turn (plugin commands keep
their own "(local time: ...)" suffix):

    [time: Fri 2026-11-13 19:20 · 1 h 7 min since the last message (18:13) · since then: 3 new mails (1 request), 1 approval resolved]

The gap is kept per channel (console, telegram; later per person) in state_dir/last_seen.json.
Events come from an append-only log, state_dir/events.jsonl, written by `record_event`. The log
holds kinds, short labels and counts only, never message content: it is read back into the
model's context, so nothing a sender wrote may end up there. The log is capped; a broken line
is skipped.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from pathlib import Path
from typing import Callable

from .statefiles import read_private_json, write_private_json

EVENTS_FILE = "events.jsonl"
LAST_SEEN_FILE = "last_seen.json"
LAST_SEEN_VERSION = 1
MAX_LOG_BYTES = 256 * 1024
KEEP_LINES = 1000
GAP_MIN_S = 60  # below a minute the gap is not worth a mention

# kind -> (singular, plural) for the summary; unknown kinds are counted under their own name
KINDS = {
    "mail.new": ("new mail", "new mails"),
    "approval.resolved": ("approval resolved", "approvals resolved"),
    "approval.new": ("new approval", "new approvals"),
    "task.done": ("background task done", "background tasks done"),
    "task.failed": ("background task failed", "background tasks failed"),
    "brief.sent": ("brief sent", "briefs sent"),
}
_KIND_RE = re.compile(r"[a-z]{1,16}(\.[a-z]{1,16})?")
_LABEL_RE = re.compile(r"[a-z][a-z0-9_-]{0,23}")


def record_event(state_dir: Path | str, kind: str, label: str | None = None, *, now: float | None = None) -> None:
    """Append one event. `label` is a fixed category word (e.g. a mail category), never free text."""
    if not _KIND_RE.fullmatch(kind):
        raise ValueError("event kind must look like 'mail.new'")
    entry: dict = {"ts": round(time.time() if now is None else now, 3), "kind": kind}
    if label is not None:
        if not _LABEL_RE.fullmatch(label):
            raise ValueError("event label must be a short lowercase word")
        entry["label"] = label
    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / EVENTS_FILE
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, separators=(",", ":")) + "\n")
    try:
        if path.stat().st_size > MAX_LOG_BYTES:
            _trim(path)
    except OSError:
        pass


def _trim(path: Path) -> None:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-KEEP_LINES:]
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


def read_events(state_dir: Path | str, since: float) -> list[dict]:
    """Events newer than `since`, oldest first. Broken or unexpected lines are skipped."""
    path = Path(state_dir) / EVENTS_FILE
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or not isinstance(entry.get("ts"), (int, float)):
            continue
        kind, label = entry.get("kind"), entry.get("label")
        if not isinstance(kind, str) or not _KIND_RE.fullmatch(kind):
            continue
        if label is not None and (not isinstance(label, str) or not _LABEL_RE.fullmatch(label)):
            label = None
        if entry["ts"] > since:
            out.append({"ts": float(entry["ts"]), "kind": kind, "label": label})
    return out


def format_gap(seconds: float) -> str:
    minutes = int(seconds // 60)
    days, rest = divmod(minutes, 24 * 60)
    hours, mins = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days} d")
    if hours:
        parts.append(f"{hours} h")
    if mins and not days:
        parts.append(f"{mins} min")
    return " ".join(parts) or "0 min"


def summarize(events: list[dict]) -> str:
    """'3 new mails (1 request), 1 approval resolved' — counts per kind in a stable order."""
    by_kind = Counter(e["kind"] for e in events)
    order = [k for k in KINDS if k in by_kind] + sorted(k for k in by_kind if k not in KINDS)
    parts = []
    for kind in order:
        n = by_kind[kind]
        one, many = KINDS.get(kind, (kind, kind))
        text = f"{n} {one if n == 1 else many}"
        labels = Counter(e["label"] for e in events if e["kind"] == kind and e["label"])
        if labels:
            text += " (" + ", ".join(f"{c} {lab}" for lab, c in sorted(labels.items(), key=lambda x: (-x[1], x[0]))) + ")"
        parts.append(text)
    return ", ".join(parts)


class TimeAwareness:
    """Builds the time note for one channel and remembers when that channel last spoke."""

    def __init__(self, state_dir: Path | str, channel: str = "console", clock: Callable[[], float] = time.time) -> None:
        self.state_dir = Path(state_dir)
        self.channel = channel if _LABEL_RE.fullmatch(channel) else "console"
        self.clock = clock

    def _last_seen(self) -> dict:
        data = read_private_json(self.state_dir, LAST_SEEN_FILE, 16 * 1024, LAST_SEEN_VERSION) or {}
        seen = data.get("seen")
        return dict(seen) if isinstance(seen, dict) else {}

    def note(self) -> str:
        now = self.clock()
        seen = self._last_seen()
        last = seen.get(self.channel)
        parts = [time.strftime("%a %Y-%m-%d %H:%M", time.localtime(now))]
        if isinstance(last, (int, float)) and 0 <= now - last:
            if now - last >= GAP_MIN_S:
                parts.append(f"{format_gap(now - last)} since the last message ({time.strftime('%H:%M', time.localtime(last))})")
            events = summarize(read_events(self.state_dir, last))
            parts.append(f"since then: {events}" if events else "nothing new since then")
        seen[self.channel] = now
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_private_json(self.state_dir, LAST_SEEN_FILE, {"version": LAST_SEEN_VERSION, "seen": seen})
        return "[time: " + " · ".join(parts) + "]"


__all__ = ["TimeAwareness", "format_gap", "read_events", "record_event", "summarize"]
