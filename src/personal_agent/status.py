"""Read-only view of Claude Code session status.

Sessions report their state by writing small JSON files into a status
directory (see `hooks/claude_status_hook.py`). This module only *reads* them.
Everything read here is treated as untrusted text: it is cleaned, shortened
and only ever read aloud, never interpreted as an instruction.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

STATES = ("waiting", "working", "idle", "error")
MAX_TEXT = 160
MAX_FILE_BYTES = 16 * 1024

_UNSAFE = re.compile(r"[^\w .,:;!?()'/+\-]", re.UNICODE)
_SPACES = re.compile(r"\s+")


def clean_text(value: object, limit: int = MAX_TEXT) -> str:
    """Reduce arbitrary text to something safe to speak and to log.

    Drops control characters and markup, collapses whitespace and cuts the
    length. This is not a security boundary on its own: the boundary is that
    the phone channel never acts on this text.
    """
    text = str(value) if value is not None else ""
    text = _UNSAFE.sub(" ", text)
    text = _SPACES.sub(" ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


@dataclass(frozen=True)
class SessionStatus:
    session_id: str
    project: str
    state: str
    summary: str
    updated_at: float
    since: float

    def age_minutes(self, now: float | None = None) -> int:
        """Minutes in the current state."""
        return int(((now or time.time()) - self.since) // 60)


def format_status_line(s: SessionStatus, now: float | None = None) -> str:
    """One terminal line per session: state, minutes in that state, project, summary."""
    return f"{s.state:8} {s.age_minutes(now):4} min  {s.project:20} {s.summary}"


def _parse(path: Path) -> SessionStatus | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    state = data.get("state")
    updated_at = data.get("updated_at")
    since = data.get("since")
    if state not in STATES or not isinstance(updated_at, (int, float)):
        return None
    return SessionStatus(
        session_id=clean_text(data.get("session_id") or path.stem, 64),
        project=clean_text(data.get("project") or "unknown", 48),
        state=state,
        summary=clean_text(data.get("summary") or ""),
        updated_at=float(updated_at),
        since=float(since) if isinstance(since, (int, float)) and since <= updated_at else float(updated_at),
    )


def read_statuses(status_dir: Path, max_age_min: int, now: float | None = None) -> list[SessionStatus]:
    """All recent session statuses, waiting/error first, then newest first."""
    now = now or time.time()
    if not status_dir.is_dir():
        return []
    result = []
    for path in status_dir.glob("*.json"):
        status = _parse(path)
        if status is None:
            continue
        if now - status.updated_at > max_age_min * 60:
            continue
        result.append(status)
    priority = {"error": 0, "waiting": 1, "working": 2, "idle": 3}
    result.sort(key=lambda s: (priority[s.state], -s.updated_at))
    return result
