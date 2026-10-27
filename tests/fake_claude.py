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
  HOOK          -> a system "hook_started" event (SessionStart) before the answer
  SKILL <name>  -> announce a Skill tool_use {"skill": name} and ask the permission
                   tool like WRITE; answers "skill <name> loaded" or "denied"
  SLEEP <s>     -> sleep s seconds (at most 30) before answering "slept <s>"
  SLEEP_NOTERM <s> -> like SLEEP, but ignores SIGTERM (only SIGKILL stops it)
  APPEND <path> <line> -> announce an Edit tool_use that appends <line> after
                   "## Notes\n" in <path>, ask the permission tool, apply it on allow;
                   answers "appended" or "denied". "{task}" in <line> becomes the id
                   from a <task id="..."> wrapper.
  WRITE_AT <path> -> like WRITE but with an explicit path (e.g. outputs/x.md)
  DELEGATE <domain|-> <text> -> announce mcp__pa__delegate_task, ask the permission
                   tool like WRITE and, on allow, call tools/call delegate_task on the
                   same MCP server process; answers "delegated <id>", "denied" or
                   "delegate refused: <msg>"
  STRAY         -> answer normally, then emit another assistant event and result as if
                   something else (a peer message) had started a turn
  MAXTURNS      -> result with is_error and subtype error_max_turns
  other         -> assistant echoes the text (also "/<x>" messages)
A message that contains <user-message>...</user-message> or
<delegated-request ...>...</delegated-request> (helper tasks, notes context) is
interpreted by the unescaped body of the last such block (the echo still shows the
whole message).
Each result reports total_cost_usd = FAKE_CLAUDE_COST x turns so far (cumulative, like
the real CLI; default 0).
The init event reports apiKeySource from FAKE_CLAUDE_API_KEY_SOURCE (default
"none", i.e. subscription login), the tools from --tools, the MCP servers from
--mcp-config, one plugin per --plugin-dir (name from its plugin.json, source
"<name>@inline") plus a built-in plugin, and the plugin skills. Extra fields
come from FAKE_CLAUDE_EXTRA_INIT (a JSON object merged into the init event).
Arguments and environment variable names are written to FAKE_CLAUDE_LOG, one
line per process start.

`agents --json` (the read-only session listing) prints FAKE_CLAUDE_AGENTS_JSON
(default "[]") or the content of the file FAKE_CLAUDE_AGENTS_FILE, after sleeping
FAKE_CLAUDE_AGENTS_SLEEP seconds, and exits with FAKE_CLAUDE_AGENTS_EXIT (default 0).
"""

import html
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

_BLOCK = re.compile(r"<(user-message|delegated-request)\b[^>]*>\n?(.*?)\n?</\1>", re.S)


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def command_of(text):
    """The command in a message: the body of the last user-message/delegated-request block."""
    blocks = _BLOCK.findall(text)
    if not blocks:
        return text
    return html.unescape(blocks[-1][1]).strip()


class McpClient:
    """The permission tool's MCP server, kept open for several calls (like the real CLI)."""

    def __init__(self, args):
        prompt_tool = args[args.index("--permission-prompt-tool") + 1]
        _, server_name, self.tool = prompt_tool.split("__", 2)
        with open(args[args.index("--mcp-config") + 1], encoding="utf-8") as f:
            server = json.load(f)["mcpServers"][server_name]
        self.proc = subprocess.Popen(
            [server["command"], *server.get("args", [])],
            env=server.get("env", {}),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.next_id = 1
        self.call("initialize", {"protocolVersion": "2025-06-18"})
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def send(self, msg):
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def request(self, method, params):
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        return self.next_id

    def reply(self, msg_id):
        for line in self.proc.stdout:
            msg = json.loads(line)
            if msg.get("id") == msg_id:
                return msg
        raise RuntimeError("MCP server closed")

    def call(self, method, params):
        return self.reply(self.request(method, params))

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)


def ask_permission(args, tool_name, tool_input, tool_use_id, die_after_s=None, client=None):
    """Run the permission tool the way Claude Code does. None means no tool configured.

    With die_after_s, the process exits with code 3 that long after sending the
    request, while the approval is still pending (a crashing CLI). With client, that
    MCP server process is used and left open.
    """
    if "--mcp-config" not in args or "--permission-prompt-tool" not in args:
        return None
    own = client is None
    client = client or McpClient(args)
    try:
        msg_id = client.request("tools/call", {
            "name": client.tool,
            "arguments": {"tool_name": tool_name, "input": tool_input, "tool_use_id": tool_use_id},
        })
        if die_after_s is not None:
            time.sleep(die_after_s)
            os._exit(3)
        answer = client.reply(msg_id)
    finally:
        if own:
            client.close()
    return json.loads(answer["result"]["content"][0]["text"])


def tool_call(args, tool_name, tool_input, tool_use_id, path=None):
    """Announce a tool_use, ask permission; returns the verdict (None without a permission tool)."""
    emit({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tool_use_id, "name": tool_name, "input": tool_input}]}})
    return ask_permission(args, tool_name, tool_input, tool_use_id)


def option_values(args, flag):
    return [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == flag]


def init_event(args, session):
    tools = [t for t in (option_values(args, "--tools") or [""])[0].split(",") if t]
    servers = []
    for path in option_values(args, "--mcp-config"):
        with open(path, encoding="utf-8") as f:
            servers += [{"name": name, "status": "connected"} for name in json.load(f).get("mcpServers", {})]
    plugins, skills = [], []
    for plugin_dir in option_values(args, "--plugin-dir"):
        with open(os.path.join(plugin_dir, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
            name = json.load(f)["name"]
        plugins.append({"name": name, "path": plugin_dir, "source": f"{name}@inline"})
        skills_dir = Path(plugin_dir) / "skills"
        if skills_dir.is_dir():
            skills += [f"{name}:{p.name}" for p in sorted(skills_dir.iterdir())]
    plugins.append({"name": "cc-plugin-telemetry", "path": "builtin", "source": "cc-plugin-telemetry@builtin"})
    event = {"type": "system", "subtype": "init", "session_id": session, "tools": tools, "model": "fake",
             "mcp_servers": servers, "plugins": plugins, "skills": ["dataviz", *skills],
             "slash_commands": list(skills),
             "apiKeySource": os.environ.get("FAKE_CLAUDE_API_KEY_SOURCE", "none")}
    event.update(json.loads(os.environ.get("FAKE_CLAUDE_EXTRA_INIT") or "{}"))
    return event


def main():
    args = sys.argv[1:]
    log = os.environ.get("FAKE_CLAUDE_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"args": args, "env": sorted(os.environ), "cwd": os.getcwd(),
                                "t": time.time(), "pid": os.getpid()}) + "\n")
    if args[:2] == ["agents", "--json"]:
        time.sleep(float(os.environ.get("FAKE_CLAUDE_AGENTS_SLEEP") or 0))
        listing = os.environ.get("FAKE_CLAUDE_AGENTS_FILE")
        if listing:
            with open(listing, encoding="utf-8") as f:
                sys.stdout.write(f.read())
        else:
            sys.stdout.write(os.environ.get("FAKE_CLAUDE_AGENTS_JSON") or "[]")
        sys.stdout.flush()
        sys.exit(int(os.environ.get("FAKE_CLAUDE_AGENTS_EXIT") or 0))
    session =args[args.index("--resume") + 1] if "--resume" in args else str(uuid.uuid4())
    started = False
    turns_done = 0
    for line in sys.stdin:
        msg = json.loads(line)
        turns_done += 1
        raw = msg["message"]["content"]
        text = command_of(raw)
        if not started:
            emit(init_event(args, session))
            started = True
        if text == "DIE":
            sys.exit(3)
        if text == "SLOW":
            time.sleep(5)
        if text == "HOOK":
            emit({"type": "system", "subtype": "hook_started", "hook_event": "SessionStart", "session_id": session})
        denials = []
        answer = f"echo: {raw}"
        if text.startswith(("SLEEP ", "SLEEP_NOTERM ")):
            if text.startswith("SLEEP_NOTERM "):
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
            seconds = min(float(text.split()[1]), 30.0)
            time.sleep(seconds)
            answer = f"slept {text.split()[1]}"
            if log:  # the end of the sleep, so tests can assert order instead of timing
                with open(log, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"slept": seconds, "t": time.time(), "pid": os.getpid()}) + "\n")
        if text.startswith("APPEND "):
            _, path, note = text.split(" ", 2)
            task_match = re.search(r'<task id="([0-9a-f]{6})"', raw)
            if task_match:
                note = note.replace("{task}", task_match.group(1))
            tool_input = {"file_path": path, "old_string": "## Notes\n", "new_string": f"## Notes\n{note}\n"}
            tool_use_id = f"toolu_{uuid.uuid4().hex}"
            verdict = tool_call(args, "Edit", tool_input, tool_use_id)
            if verdict and verdict.get("behavior") == "allow":
                approved = verdict["updatedInput"]
                target = Path(approved["file_path"])
                target.write_text(target.read_text(encoding="utf-8").replace(
                    approved["old_string"], approved["new_string"], 1), encoding="utf-8")
                answer = "appended"
            else:
                answer = "denied"
                denials = [{"tool_name": "Edit", "tool_use_id": tool_use_id, "tool_input": tool_input}]
        if text.startswith("WRITE_AT "):
            path = text.split(" ", 1)[1]
            tool_input = {"file_path": path, "content": "x"}
            tool_use_id = f"toolu_{uuid.uuid4().hex}"
            verdict = tool_call(args, "Write", tool_input, tool_use_id)
            if verdict and verdict.get("behavior") == "allow":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                Path(path).write_text("x", encoding="utf-8")
                answer = f"wrote {path}"
            else:
                answer = "denied"
                denials = [{"tool_name": "Write", "tool_use_id": tool_use_id, "tool_input": tool_input}]
        if text.startswith("DELEGATE "):
            _, domain, task_text = text.split(" ", 2)
            tool_input = {"text": task_text} if domain == "-" else {"text": task_text, "domain": domain}
            tool_use_id = f"toolu_{uuid.uuid4().hex}"
            emit({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": tool_use_id, "name": "mcp__pa__delegate_task", "input": tool_input}]}})
            client = McpClient(args) if "--mcp-config" in args and "--permission-prompt-tool" in args else None
            try:
                verdict = ask_permission(args, "mcp__pa__delegate_task", tool_input, tool_use_id, client=client)
                if verdict and verdict.get("behavior") == "allow":
                    reply = client.call("tools/call", {"name": "delegate_task", "arguments": verdict["updatedInput"]})
                    result = reply.get("result") or {}
                    content = (result.get("content") or [{}])[0].get("text", "")
                    if result.get("isError") or "error" in reply:
                        answer = content if content.startswith("delegation refused") else "delegate refused: error"
                        answer = answer.replace("delegation refused", "delegate refused", 1)
                    else:
                        answer = f"delegated {json.loads(content)['task_id']}"
                else:
                    answer = "denied"
                    denials = [{"tool_name": "mcp__pa__delegate_task", "tool_use_id": tool_use_id,
                                "tool_input": tool_input}]
            finally:
                if client is not None:
                    client.close()
        if text == "MAXTURNS":
            emit({"type": "result", "subtype": "error_max_turns", "is_error": True, "result": "",
                  "session_id": session, "permission_denials": [], "total_cost_usd": 0.0})
            continue
        if text.startswith("SKILL "):
            name = text.split(" ", 1)[1]
            tool_input = {"skill": name}
            tool_use_id = f"toolu_{uuid.uuid4().hex}"
            emit({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": tool_use_id, "name": "Skill", "input": tool_input}]}, "session_id": session})
            verdict = ask_permission(args, "Skill", tool_input, tool_use_id)
            if verdict and verdict.get("behavior") == "allow":
                answer = f"skill {name} loaded"
            else:
                answer = "denied"
                denials = [{"tool_name": "Skill", "tool_use_id": tool_use_id, "tool_input": tool_input}]
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
            # Cumulative for the process, like the real CLI; per-turn step from FAKE_CLAUDE_COST.
            "total_cost_usd": round(float(os.environ.get("FAKE_CLAUDE_COST") or 0) * turns_done, 6),
        })
        if text == "STRAY":
            time.sleep(0.2)
            emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "peer turn"}]},
                  "session_id": session})
            emit({"type": "result", "subtype": "success", "is_error": False, "result": "peer turn",
                  "session_id": session, "permission_denials": [], "total_cost_usd": 0.0})


if __name__ == "__main__":
    main()
