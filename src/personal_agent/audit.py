"""Audit log: who decided which approval, when (state_dir/audit.jsonl).

One JSON line per decided approval of level 2 (write) or 3 (danger), and per approval that
failed before a level was known. Reads (level 1) are allowed by rule and not logged, or the
log would hold every file the assistant looked at.

A line holds the time, the session (`main` or a helper task), the channel the person answers
on (`chat`, `telegram`), the tool name, the level, allow/deny and the reason, which is one of
the broker's own fixed texts. It never holds the tool input (paths, mail text, notes) or
anything a sender wrote. The log is append-only for the agent, 0600, capped at MAX_BYTES (the
oldest lines go first); `personal-agent audit` shows the newest lines.

Later (roles, M4) the person who answered is added; today there is one owner per installation.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

AUDIT_FILE = "audit.jsonl"
MAX_BYTES = 1024 * 1024
KEEP_LINES = 5000
_NAME_RE = re.compile(r"[A-Za-z0-9_:.\-]{1,80}")
_TEXT_RE = re.compile(r"[^\x20-\x7e]")


def _name(value: object, fallback: str = "?") -> str:
    return value if isinstance(value, str) and _NAME_RE.fullmatch(value) else fallback


def _text(value: object, limit: int = 120) -> str:
    return _TEXT_RE.sub("?", str(value))[:limit] if value is not None else ""


def record_approval(state_dir: Path | str, *, scope: str, channel: str, tool: str, level: int | None,
                    allow: bool, reason: str, now: float | None = None) -> None:
    entry = {
        "ts": round(time.time() if now is None else now, 3),
        "action": "approval",
        "scope": _name(scope),
        "channel": _name(channel),
        "tool": _name(tool),
        "level": int(level) if isinstance(level, int) and 1 <= int(level) <= 3 else None,
        "decision": "allow" if allow else "deny",
        "reason": _text(reason),
    }
    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / AUDIT_FILE
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, separators=(",", ":")) + "\n")
    try:
        if path.stat().st_size > MAX_BYTES:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-KEEP_LINES:]
            tmp = path.with_name(path.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            os.replace(tmp, path)
    except OSError:
        pass


def read_audit(state_dir: Path | str, last: int = 20) -> list[dict]:
    """The newest `last` entries, oldest first; broken lines are skipped."""
    try:
        raw = (Path(state_dir) / AUDIT_FILE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("ts"), (int, float)):
            out.append(entry)
    return out[-max(1, last):]


def format_entry(entry: dict) -> str:
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry["ts"]))
    level = entry.get("level")
    return (f"{when}  {_name(entry.get('decision'))}  level {level if level else '-'}  {_name(entry.get('tool'))}"
            f"  [{_name(entry.get('scope'))} via {_name(entry.get('channel'))}]  {_text(entry.get('reason'))}")


__all__ = ["AUDIT_FILE", "format_entry", "read_audit", "record_approval"]
