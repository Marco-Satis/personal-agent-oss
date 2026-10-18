#!/usr/bin/env python3
"""Stand-in for the `claude` CLI in tests: speaks stream-json, costs nothing.

Behaviour per user message content:
  FAIL  -> result with is_error
  DIE   -> exit without a result
  SLOW  -> sleep 5 s before answering
  other -> assistant echoes the text
Arguments and selected environment variables are written to FAKE_CLAUDE_LOG.
"""

import json
import os
import sys
import time
import uuid


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    args = sys.argv[1:]
    log = os.environ.get("FAKE_CLAUDE_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"args": args, "env": sorted(os.environ), "cwd": os.getcwd()}) + "\n")
    session = args[args.index("--resume") + 1] if "--resume" in args else str(uuid.uuid4())
    started = False
    for line in sys.stdin:
        msg = json.loads(line)
        text = msg["message"]["content"]
        if not started:
            emit({"type": "system", "subtype": "init", "session_id": session, "tools": [], "model": "fake"})
            started = True
        if text == "DIE":
            sys.exit(3)
        if text == "SLOW":
            time.sleep(5)
        emit({"type": "assistant", "message": {"content": [{"type": "text", "text": f"echo: {text}"}]}, "session_id": session})
        emit({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {
            "five_hour": {"utilization": 0.25}, "seven_day": {"utilization": 0.4}}}})
        emit({
            "type": "result",
            "subtype": "error_during_execution" if text == "FAIL" else "success",
            "is_error": text == "FAIL",
            "result": f"echo: {text}",
            "session_id": session,
            "permission_denials": [],
            "total_cost_usd": 0.0,
        })


if __name__ == "__main__":
    main()
