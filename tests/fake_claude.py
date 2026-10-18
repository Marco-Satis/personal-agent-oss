#!/usr/bin/env python3
"""Stand-in for the `claude` CLI in tests: speaks stream-json, costs nothing.

Behaviour per user message content:
  FAIL          -> result with is_error
  DIE           -> exit without a result
  SLOW          -> sleep 5 s before answering
  WRITE <name>  -> announce a Write tool_use (assistant event), then ask the
                   permission tool like the real CLI: start the MCP server from
                   --mcp-config, run initialize and `tools/call` on the tool named
                   by --permission-prompt-tool with
                   {"tool_name": "Write", "input": {...}, "tool_use_id": ...}.
                   On allow it writes <name> in its cwd with the returned
                   updatedInput and answers "wrote <name>"; otherwise (deny, or
                   no --mcp-config) it answers "denied" with a permission denial.
  WRITE_DIE <name> -> like WRITE, but exits (code 3) 1 s after sending the
                   permission request, while the approval is still pending
  OVERAGE       -> the rate_limit_event reports isUsingOverage, then SLOW-style
                   5 s pause before the answer (the caller should abort first)
  other         -> assistant echoes the text
The init event reports apiKeySource from FAKE_CLAUDE_API_KEY_SOURCE (default
"none", i.e. subscription login).
Arguments and environment variable names are written to FAKE_CLAUDE_LOG, one
line per process start.
"""

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def ask_permission(args, tool_name, tool_input, tool_use_id, die_after_s=None):
    """Run the permission tool the way Claude Code does. None means no tool configured.

    With die_after_s, the process exits with code 3 that long after sending the
    request, while the approval is still pending (a crashing CLI).
    """
    if "--mcp-config" not in args or "--permission-prompt-tool" not in args:
        return None
    prompt_tool = args[args.index("--permission-prompt-tool") + 1]
    _, server_name, tool = prompt_tool.split("__", 2)
    with open(args[args.index("--mcp-config") + 1], encoding="utf-8") as f:
        server = json.load(f)["mcpServers"][server_name]
    proc = subprocess.Popen(
        [server["command"], *server.get("args", [])],
        env=server.get("env", {}),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )

    def send(msg):
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def reply(msg_id):
        for line in proc.stdout:
            msg = json.loads(line)
            if msg.get("id") == msg_id:
                return msg
        raise RuntimeError("MCP server closed")

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        reply(1)
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": tool,
            "arguments": {"tool_name": tool_name, "input": tool_input, "tool_use_id": tool_use_id},
        }})
        if die_after_s is not None:
            time.sleep(die_after_s)
            os._exit(3)
        answer = reply(2)
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)
    return json.loads(answer["result"]["content"][0]["text"])


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
            emit({"type": "system", "subtype": "init", "session_id": session, "tools": [], "model": "fake",
                  "apiKeySource": os.environ.get("FAKE_CLAUDE_API_KEY_SOURCE", "none")})
            started = True
        if text == "DIE":
            sys.exit(3)
        if text == "SLOW":
            time.sleep(5)
        denials = []
        answer = f"echo: {text}"
        if text.startswith(("WRITE ", "WRITE_DIE ")):
            name = text.split(" ", 1)[1]
            tool_input = {"file_path": name, "content": "x"}
            tool_use_id = f"toolu_{uuid.uuid4().hex}"
            emit({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": tool_use_id, "name": "Write", "input": tool_input}]}, "session_id": session})
            die_after_s = 1.0 if text.startswith("WRITE_DIE ") else None
            verdict = ask_permission(args, "Write", tool_input, tool_use_id, die_after_s)
            if verdict and verdict.get("behavior") == "allow":
                approved = verdict["updatedInput"]
                Path(approved["file_path"]).write_text(approved["content"], encoding="utf-8")
                answer = f"wrote {name}"
            else:
                answer = "denied"
                denials = [{"tool_name": "Write", "tool_use_id": tool_use_id, "tool_input": tool_input}]
        if text == "OVERAGE":
            emit({"type": "rate_limit_event", "rate_limit_info": {
                "status": "allowed", "isUsingOverage": True, "resetsAt": int(time.time()) + 3600}})
            time.sleep(5)
        emit({"type": "assistant", "message": {"content": [{"type": "text", "text": answer}]}, "session_id": session})
        emit({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {
            "five_hour": {"utilization": 0.25}, "seven_day": {"utilization": 0.4}}}})
        emit({
            "type": "result",
            "subtype": "error_during_execution" if text == "FAIL" else "success",
            "is_error": text == "FAIL",
            "result": answer,
            "session_id": session,
            "permission_denials": denials,
            "total_cost_usd": 0.0,
        })


if __name__ == "__main__":
    main()
