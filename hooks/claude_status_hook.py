#!/usr/bin/env python3
"""Claude Code hook: report the session state to the personal agent.

Writes one small JSON file per session into the status directory
(PA_STATUS_DIR, default ~/.local/state/personal-agent/sessions). The agent only
reads these files. The hook never blocks Claude Code: it always exits 0.

Register it for the events UserPromptSubmit, Notification, Stop and SessionEnd
(see README.md for the settings.json snippet).
"""

import json
import os
import sys
import time
from pathlib import Path

STATE_BY_EVENT = {
    "UserPromptSubmit": "working",
    "PreToolUse": "working",
    "Notification": "waiting",
    "Stop": "idle",
}


def status_dir() -> Path:
    configured = os.environ.get("PA_STATUS_DIR")
    if configured:
        return Path(configured).expanduser()
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "personal-agent" / "sessions"


def main() -> None:
    data = json.load(sys.stdin)
    session_id = "".join(c for c in str(data.get("session_id", "")) if c.isalnum() or c == "-")[:64]
    if not session_id:
        return
    event = data.get("hook_event_name", "")
    target = status_dir() / f"{session_id}.json"

    if event == "SessionEnd":
        target.unlink(missing_ok=True)
        return
    state = STATE_BY_EVENT.get(event)
    if state is None:
        return

    now = time.time()
    since = now
    try:
        previous = json.loads(target.read_text(encoding="utf-8"))
        if previous.get("state") == state:
            since = float(previous.get("since", now))
    except (OSError, ValueError, TypeError, AttributeError):
        pass

    summary = str(data.get("message", ""))[:200] if state == "waiting" else ""
    record = {
        "session_id": session_id,
        "project": Path(str(data.get("cwd", ""))).name or "unknown",
        "state": state,
        "summary": summary,
        "since": since,
        "updated_at": now,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(record), encoding="utf-8")
    tmp.replace(target)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # a status hook must never break the session
        pass
    sys.exit(0)
