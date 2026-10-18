"""Read-only view of Claude Code session status.

Sessions report their state by writing small JSON files into a status
directory (see `hooks/claude_status_hook.py`). This module only *reads* them.
Everything read here is treated as untrusted text: it is cleaned, shortened
and only ever read aloud, never interpreted as an instruction.

The chat's `sessions` command can also ask Claude Code itself through the
documented `claude agents --json` (opt-in, `PA_SESSIONS_SOURCE`). That listing
covers every project of the person, so it is a privacy boundary: it is read only
for that typed command, never logged, never sent to Discord or the phone, and
only the basename of each working directory is kept. Nothing here ever sends
anything into a session (see docs/SESSION-CONTROL.md).
"""

from __future__ import annotations

import json
import os
import re
import selectors
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Mapping

STATES = ("waiting", "working", "idle", "error")
MAX_TEXT = 160
MAX_FILE_BYTES = 16 * 1024
SESSION_SOURCES = ("hooks", "agents", "both")
SESSION_ORDER = {"waiting": 0, "error": 1, "working": 2, "idle": 3}
MAX_SESSION_LINES = 20
EMPTY_SESSIONS = "no session status (install the hook: personal-agent hooks snippet)"
AGENTS_TIMEOUT_S = 5.0
AGENTS_MAX_BYTES = 256 * 1024
AGENTS_MAX_ENTRIES = 100
AGENTS_CACHE_S = 10.0
# The documented values of `waitingFor` (agent view, 2026-10-18); anything else shows as "waiting".
AGENT_WAITING_FOR = ("permission prompt", "input needed", "sandbox request", "worker request", "dialog open")
AGENT_STATUS = {"busy": "working", "waiting": "waiting", "idle": "idle"}
AGENT_STATE = {"working": "working", "blocked": "waiting", "failed": "error", "done": "idle", "stopped": "idle"}
AGENT_KINDS = ("interactive", "background")
# Only these variables reach `claude agents`: no API keys, no tokens, and never the
# assistant's private PA_CLAUDE_CONFIG_DIR (CLAUDE_CONFIG_DIR only as the person set it).
AGENT_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "CLAUDE_CONFIG_DIR")

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


# --- the chat's `sessions` view ------------------------------------------------------


@dataclass(frozen=True)
class SessionView:
    """One of the person's Claude Code sessions as the `sessions` command shows it."""

    source: str              # "hook" | "agents" | "hook+agents"
    session_id: str          # cleaned, at most 64 characters
    project: str             # basename of the working directory only, never the full path
    state: str               # one of STATES
    detail: str              # hook summary, or agents waitingFor / name; cleaned
    since: float | None      # hook: in this state since; agents: session start
    kind: str | None = None  # "interactive" | "background" | None


def views_from_statuses(statuses: Iterable[SessionStatus]) -> list[SessionView]:
    """Hook status files as views, waiting sessions first."""
    return sort_sessions(
        SessionView("hook", s.session_id, s.project, s.state, s.summary, s.since, None) for s in statuses
    )


def sort_sessions(views: Iterable[SessionView]) -> list[SessionView]:
    """Waiting first, then error, working, idle; within a state the newest first."""
    return sorted(views, key=lambda v: (SESSION_ORDER.get(v.state, len(SESSION_ORDER)), -(v.since or 0.0)))


def format_session_line(v: SessionView, now: float | None = None) -> str:
    age = "-" if v.since is None else str(max(0, int(((now or time.time()) - v.since) // 60)))
    return f"{v.state:8} {age:>4} min  {v.project:20} {v.kind or '':11} {v.detail}".rstrip()


def render_sessions(views: list[SessionView], problem: str | None = None, now: float | None = None) -> str:
    """The tier-1 answer: a header, at most MAX_SESSION_LINES lines, and an agents problem if any."""
    lines: list[str] = []
    if not views:
        lines.append(EMPTY_SESSIONS)
    else:
        ordered = sort_sessions(views)
        waiting = sum(1 for v in ordered if v.state == "waiting")
        lines.append(f"{len(ordered)} sessions ({waiting} waiting)")
        lines += [format_session_line(v, now) for v in ordered[:MAX_SESSION_LINES]]
        if len(ordered) > MAX_SESSION_LINES:
            lines.append(f"… and {len(ordered) - MAX_SESSION_LINES} more")
    if problem:
        lines.append(f"[agents: {clean_text(problem, 120)}]")
    return "\n".join(lines)


def merge_sessions(hook: list[SessionView], agents: list[SessionView]) -> list[SessionView]:
    """Join both sources by session id. Claude Code's own state wins (it is fresher)."""
    by_id = {v.session_id: v for v in agents if v.session_id}
    used: set[str] = set()
    merged: list[SessionView] = []
    for h in hook:
        a = by_id.get(h.session_id) if h.session_id else None
        if a is None or h.session_id in used:
            merged.append(h)
            continue
        used.add(h.session_id)
        merged.append(SessionView(
            source="hook+agents",
            session_id=h.session_id,
            project=a.project if a.project != "unknown" else h.project,
            state=a.state,
            detail=a.detail or h.detail,
            since=h.since if h.state == a.state else a.since,
            kind=a.kind,
        ))
    merged += [a for a in agents if not (a.session_id and a.session_id in used)]
    return sort_sessions(merged)


def _started_at(value: object) -> float | None:
    """startedAt as epoch seconds: a number (seconds or milliseconds) or an ISO 8601 string."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number != number or number <= 0:  # NaN or nonsense
            return None
        return number / 1000.0 if number > 1e12 else number
    if isinstance(value, str) and len(value) <= 40:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.timestamp()
    return None


def _agent_view(entry: object) -> SessionView | None:
    """One `claude agents --json` entry as a view, or None when it cannot be mapped."""
    if not isinstance(entry, dict):
        return None
    status, state_field = entry.get("status"), entry.get("state")
    if isinstance(status, str) and status in AGENT_STATUS:
        state = AGENT_STATUS[status]
    elif isinstance(state_field, str) and state_field in AGENT_STATE:
        state = AGENT_STATE[state_field]
    else:
        return None
    waiting_for = entry.get("waitingFor")
    name = entry.get("name")
    if state == "waiting" and isinstance(waiting_for, str) and waiting_for in AGENT_WAITING_FOR:
        detail = waiting_for
    elif isinstance(name, str) and clean_text(name):
        detail = clean_text(name)
    elif state == "waiting":
        detail = "blocked" if state_field == "blocked" and status is None else "waiting"
    else:
        detail = ""
    session_id = entry.get("sessionId") or entry.get("id")
    cwd = entry.get("cwd")
    project = os.path.basename(cwd.rstrip("/\\")) if isinstance(cwd, str) else ""
    kind = entry.get("kind")
    return SessionView(
        source="agents",
        session_id=clean_text(session_id, 64) if isinstance(session_id, str) else "",
        project=clean_text(project, 48) or "unknown",
        state=state,
        detail=detail,
        since=_started_at(entry.get("startedAt")),
        kind=kind if kind in AGENT_KINDS else None,
    )


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL the child's own process group, only while the child is not yet reaped."""
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def _run_agents(claude_bin: str, timeout_s: float, popen: Callable, env: dict[str, str]
                ) -> tuple[bytes | None, str | None]:
    """stdout of `claude agents --json` (capped), or a problem. The child never outlives the call."""
    try:
        proc = popen(
            [claude_bin, "agents", "--json"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    except (OSError, ValueError):
        return None, "claude agents could not be started"
    deadline = time.monotonic() + timeout_s
    chunks: list[bytes] = []
    size = 0
    problem: str | None = None
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            fd = proc.stdout.fileno()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    problem = f"claude agents did not answer within {timeout_s:g} s"
                    break
                if not selector.select(remaining):
                    continue
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > AGENTS_MAX_BYTES:
                    problem = "claude agents printed too much output"
                    break
                chunks.append(chunk)
        if problem is None:
            try:
                code = proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                problem = f"claude agents did not answer within {timeout_s:g} s"
            else:
                if code != 0:
                    problem = f"claude agents failed (exit {code})"
    except (OSError, ValueError):
        problem = "claude agents could not be read"
    finally:
        if problem is not None or proc.poll() is None:
            _kill_group(proc)
        try:
            proc.stdout.close()
        except (OSError, AttributeError):
            pass
    return (None, problem) if problem else (b"".join(chunks), None)


_agent_cache: dict[tuple[str, str | None], tuple[float, list[SessionView], str | None]] = {}
_agent_cache_lock = threading.Lock()


def clear_agent_cache() -> None:
    with _agent_cache_lock:
        _agent_cache.clear()


def read_agent_sessions(
    claude_bin: str,
    timeout_s: float = AGENTS_TIMEOUT_S,
    popen: Callable = subprocess.Popen,
    environ: Mapping[str, str] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[list[SessionView], str | None]:
    """The person's sessions from the documented `claude agents --json` (views, problem).

    Read-only. Call it only from the typed tier-1 `sessions` command: the listing
    covers every project, so it is never logged, never sent to Discord or the phone,
    and keeps only the basename of each working directory. Results are cached for
    AGENTS_CACHE_S seconds per process. Agent view is a research preview; whether this
    call starts its supervisor is not documented, so the source is off by default.
    """
    source_env = os.environ if environ is None else environ
    env = {name: source_env[name] for name in AGENT_ENV if source_env.get(name)}
    key = (claude_bin, env.get("CLAUDE_CONFIG_DIR"))
    now = clock()
    with _agent_cache_lock:
        cached = _agent_cache.get(key)
        if cached is not None and now - cached[0] < AGENTS_CACHE_S:
            return list(cached[1]), cached[2]
    raw, problem = _run_agents(claude_bin, timeout_s, popen, env)
    views: list[SessionView] = []
    if raw is not None:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            data, problem = None, "claude agents printed no valid JSON"
        if problem is None and not isinstance(data, list):
            problem = "claude agents printed no list"
        elif problem is None and len(data) > AGENTS_MAX_ENTRIES:
            problem = f"claude agents listed more than {AGENTS_MAX_ENTRIES} sessions"
        elif problem is None:
            views = [v for v in (_agent_view(entry) for entry in data) if v is not None]
    with _agent_cache_lock:
        _agent_cache[key] = (now, list(views), problem)
    return views, problem


def session_overview(
    status_dir: Path,
    max_age_min: int,
    source: str = "hooks",
    claude_bin: str = "claude",
    now: float | None = None,
    agents_reader: Callable[[str], tuple[list[SessionView], str | None]] | None = None,
) -> str:
    """The `sessions` answer from the configured sources (hooks, agents or both)."""
    views: list[SessionView] = []
    problem = None
    if source in ("hooks", "both"):
        views = views_from_statuses(read_statuses(status_dir, max_age_min, now))
    if source in ("agents", "both"):
        agents, problem = (agents_reader or read_agent_sessions)(claude_bin)
        views = merge_sessions(views, agents)
    return render_sessions(views, problem, now)
