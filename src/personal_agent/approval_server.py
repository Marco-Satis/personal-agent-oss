"""Stdio MCP server with one tool, `approve`, used as `--permission-prompt-tool mcp__pa__approve`.

Claude Code calls `approve` before it runs a tool that needs permission. This
process forwards each request over a Unix domain socket to the agent process,
which decides (and asks the person when needed) and answers allow or deny.
Every failure ends in a deny.

The agent starts this file as `python -I -S <absolute path>`, so nothing in the
assistant's working directory (a planted `json.py` or `personal_agent/`) and no
`.pth` file in a site-packages directory can replace or patch it. It therefore
imports only the standard library and nothing from
its own package. `python -m personal_agent.approval_server` still works for
manual use.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import threading

PROTOCOL = 1
SERVER_NAME = "pa"
TOOL_NAME = "approve"
ENV_SOCKET = "PA_APPROVAL_SOCKET"
ENV_TOKEN = "PA_APPROVAL_TOKEN"
ENV_TIMEOUT = "PA_APPROVAL_TIMEOUT_S"
ENV_PREFIX = "PA_APPROVAL_"
MAX_FRAME_BYTES = 256 * 1024
MAX_REPLY_BYTES = 16 * 1024
MAX_STR_CHARS = 2000      # strings forwarded to the broker are cut to this length
MAX_PATH_CHARS = 4096     # path keys are never cut; longer means deny
MAX_ITEMS = 50
MAX_DEPTH = 4
PATH_KEYS = ("file_path", "notebook_path", "path")
CONNECT_TIMEOUT_S = 5
DEFAULT_TIMEOUT_S = 300
DEFAULT_PROTOCOL_VERSION = "2025-06-18"

TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": "Permission prompt handler for the personal agent.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "tool_name": {"type": "string"},
            "input": {"type": "object"},
            "tool_use_id": {"type": "string"},
        },
        "required": ["tool_name", "input"],
    },
}


def deny(message: str) -> dict:
    return {"behavior": "deny", "message": message}


def allow(original_input: dict) -> dict:
    """Built from this process's own copy of the input, so it is never modified."""
    return {"behavior": "allow", "updatedInput": original_input}


def input_digest(tool_input: object) -> str:
    """Stable hash of a tool input; the broker compares it with the announced tool call."""
    text = json.dumps(tool_input, sort_keys=True, ensure_ascii=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def shrink(value: object, depth: int = 0) -> object:
    """JSON-safe, size-limited copy of a value for the broker."""
    if isinstance(value, str):
        return value[:MAX_STR_CHARS]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (list, tuple, dict)) and depth >= MAX_DEPTH:
        return "…"
    if isinstance(value, (list, tuple)):
        return [shrink(item, depth + 1) for item in list(value)[:MAX_ITEMS]]
    if isinstance(value, dict):
        return {str(k): shrink(v, depth + 1) for k, v in list(value.items())[:MAX_ITEMS]}
    return str(value)[:MAX_STR_CHARS]


def shrink_input(tool_input: dict) -> dict:
    """Top level: every key is kept, path values are kept whole, the rest is shrunk."""
    return {
        str(key): value if key in PATH_KEYS and isinstance(value, str) else shrink(value, 1)
        for key, value in tool_input.items()
    }


def _read_line(sock: socket.socket, limit: int) -> bytes:
    with sock.makefile("rb") as stream:
        line = stream.readline(limit + 1)
    if not line.endswith(b"\n") or len(line) > limit:
        raise ValueError("bad reply frame")
    return line


def ask_broker(
    socket_path: str,
    token: str,
    tool_name: object,
    tool_input: object,
    tool_use_id: object,
    timeout_s: float,
) -> dict:
    """Forward one request to the agent process and return the verdict."""
    if not isinstance(tool_name, str) or not tool_name or not isinstance(tool_input, dict):
        return deny("invalid permission request")
    if len(tool_input) > MAX_ITEMS:
        return deny("too many fields to review")
    for key in PATH_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and len(value) > MAX_PATH_CHARS:
            return deny("path too long to review")
    if not socket_path or not token:
        return deny("approval service unreachable")
    frame = {
        "v": PROTOCOL,
        "token": token,
        "tool_name": tool_name,
        "input": shrink_input(tool_input),
        "tool_use_id": tool_use_id if isinstance(tool_use_id, str) else None,
        "input_sha256": input_digest(tool_input),
    }
    data = (json.dumps(frame, ensure_ascii=True) + "\n").encode("ascii")
    if len(data) > MAX_FRAME_BYTES:
        return deny("request too large to review")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(CONNECT_TIMEOUT_S)
            sock.connect(socket_path)
            sock.settimeout(max(float(timeout_s), 0.0) + 15)
            sock.sendall(data)
            answer = json.loads(_read_line(sock, MAX_REPLY_BYTES))
    except (OSError, ValueError):
        return deny("approval service unreachable")
    if not isinstance(answer, dict) or answer.get("v") != PROTOCOL:
        return deny("approval service unreachable")
    if answer.get("allow") is True:
        return allow(tool_input)
    message = answer.get("message")
    return deny(message if isinstance(message, str) and message else "not approved")


def _timeout(env: dict) -> float:
    try:
        value = float(env.get(ENV_TIMEOUT) or DEFAULT_TIMEOUT_S)
    except ValueError:
        return DEFAULT_TIMEOUT_S
    return value if 0 < value <= 24 * 3600 else DEFAULT_TIMEOUT_S


def _error(msg_id: object, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _call_tool(params: dict, env: dict) -> dict:
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        verdict = deny("invalid permission request")
    else:
        try:
            verdict = ask_broker(
                env.get(ENV_SOCKET) or "",
                env.get(ENV_TOKEN) or "",
                arguments.get("tool_name"),
                arguments.get("input"),
                arguments.get("tool_use_id"),
                _timeout(env),
            )
        except Exception:  # noqa: BLE001 - any failure must end in a deny
            verdict = deny("approval service failed")
    return {"content": [{"type": "text", "text": json.dumps(verdict)}]}


def handle(msg: dict, env: dict) -> dict | None:
    """Pure JSON-RPC dispatch. Notifications (no "id") get no reply."""
    if "id" not in msg or "method" not in msg:
        return None
    msg_id = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    if method == "initialize":
        result = {
            "protocolVersion": params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": "1"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [TOOL_SCHEMA]}
    elif method == "tools/call":
        if params.get("name") != TOOL_NAME:
            return _error(msg_id, -32602, "unknown tool")
        result = _call_tool(params, env)
    else:
        return _error(msg_id, -32601, "method not found")
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def scrub_environ(environ) -> None:
    """Drop every variable except our own, so no inherited secret lingers in this helper."""
    for key in list(environ):
        if not key.startswith(ENV_PREFIX):
            environ.pop(key, None)


def main() -> int:
    env = {key: os.environ.get(key, "") for key in (ENV_SOCKET, ENV_TOKEN, ENV_TIMEOUT)}
    scrub_environ(os.environ)
    if not env[ENV_SOCKET] or not env[ENV_TOKEN]:
        print("approval server: socket or token missing, every request is denied", file=sys.stderr)
    out_lock = threading.Lock()

    def write(obj: dict) -> None:
        line = json.dumps(obj, ensure_ascii=True) + "\n"
        with out_lock:
            sys.stdout.write(line)
            sys.stdout.flush()

    def run(msg: dict) -> None:
        try:
            reply = handle(msg, env)
        except Exception:  # noqa: BLE001
            reply = _error(msg.get("id"), -32603, "internal error") if "id" in msg else None
        if reply is not None:
            write(reply)

    for raw in sys.stdin.buffer:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            write(_error(None, -32700, "parse error"))
            continue
        if not isinstance(msg, dict):
            write(_error(None, -32600, "invalid request"))
            continue
        if msg.get("method") == "tools/call" and "id" in msg:
            # Parallel tool uses must not wait for each other's approval.
            threading.Thread(target=run, args=(msg,), daemon=True).start()
        else:
            run(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
