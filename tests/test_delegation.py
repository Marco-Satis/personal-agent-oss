import json
import shlex
import stat
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from personal_agent import approval, approval_server
from personal_agent.approval import ApprovalBroker, Level, classify
from personal_agent.approval_server import (
    DELEGATE_TOOL_NAME,
    ENV_DELEGATE,
    SERVER_NAME,
    ask_broker,
    ask_broker_delegate,
    handle,
)
from personal_agent.bridge import DELEGATE_TOOL, BridgeConfig
from personal_agent.router import init_problem

FAKE = Path(__file__).resolve().parent / "fake_claude.py"


class Channel:
    def __init__(self, answer=True):
        self.answer = answer
        self.calls = []

    def ask(self, request, deadline):
        self.calls.append(request)
        return self.answer


@pytest.fixture
def broker(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    b = ApprovalBroker(work, channel=Channel(), bind_wait_s=0.3)
    b.enable_delegation()
    b.queued = []

    def on_delegate(text, domain):
        b.queued.append((text, domain))
        return "abc123"

    b.on_delegate = on_delegate
    b.start(tmp_path / "run")
    b.begin_turn()
    yield b
    b.close()


def announce(broker, tool_input, tool_use_id=None):
    tool_use_id = tool_use_id or f"toolu_{time.monotonic_ns()}"
    broker.observe({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tool_use_id, "name": DELEGATE_TOOL, "input": tool_input}]}})
    return tool_use_id


def approve(broker, tool_input):
    return broker.decide(DELEGATE_TOOL, tool_input, announce(broker, tool_input))


def call(broker, tool_input, token=None):
    return ask_broker_delegate(str(broker.socket_path), token or broker.token, tool_input, 5)


def test_names_match():
    assert DELEGATE_TOOL == f"mcp__{SERVER_NAME}__{DELEGATE_TOOL_NAME}"
    assert classify(DELEGATE_TOOL, {"text": "x"}) == Level.WRITE


def test_tool_is_offered_only_with_the_flag():
    listed = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, {})
    assert [t["name"] for t in listed["result"]["tools"]] == ["approve"]
    listed = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, {ENV_DELEGATE: "1"})
    assert [t["name"] for t in listed["result"]["tools"]] == ["approve", "delegate_task"]
    schema = listed["result"]["tools"][1]["inputSchema"]
    assert schema["properties"]["text"]["maxLength"] == 300 and schema["additionalProperties"] is False
    refused = handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "delegate_task", "arguments": {"text": "x"}}}, {})
    assert refused["error"]["message"] == "unknown tool"


def test_main_mcp_config_carries_the_flag_helpers_never(broker):
    env = json.loads(broker.mcp_config_path.read_text())["mcpServers"]["pa"]["env"]
    assert env[ENV_DELEGATE] == "1"
    scope = broker.open_scope("task-aaaaaa", task_id="aaaaaa")
    helper_env = json.loads(scope.mcp_config_path.read_text())["mcpServers"]["pa"]["env"]
    assert ENV_DELEGATE not in helper_env


def test_approved_delegation_queues_once(broker):
    data = {"text": "write a summary of the backup notes", "domain": "research"}
    assert approve(broker, data).allow
    request = broker.channel.calls[0]
    assert request.level == Level.WRITE and "queue a background task" in request.summary
    assert json.dumps(data["text"]) in request.summary
    result = call(broker, data)
    assert "isError" not in result
    assert json.loads(result["content"][0]["text"]) == {
        "task_id": "abc123", "status": "queued", "note": approval_server.DELEGATE_NOTE}
    assert broker.queued == [("write a summary of the backup notes", "research")]
    replay = call(broker, data)  # one-shot
    assert replay["isError"] and "not approved" in replay["content"][0]["text"]
    assert len(broker.queued) == 1


def test_delegation_without_approval_is_refused(broker):
    result = call(broker, {"text": "sneaky"})
    assert result["isError"] and "not approved" in result["content"][0]["text"]
    assert broker.queued == []


def test_denied_delegation_queues_nothing(broker):
    broker.channel.answer = False
    data = {"text": "do it"}
    assert not approve(broker, data).allow
    assert call(broker, data)["isError"] and broker.queued == []


def test_one_delegation_per_turn(broker):
    assert approve(broker, {"text": "first"}).allow
    second = approve(broker, {"text": "second"})
    assert second.message == "one delegation per turn" and len(broker.channel.calls) == 1


def test_approval_does_not_carry_over_to_the_next_turn(broker):
    data = {"text": "later"}
    assert approve(broker, data).allow
    broker.begin_turn()
    assert call(broker, data)["isError"] and broker.queued == []


@pytest.mark.parametrize("data, message", [
    ({"text": "x" * 301}, "1 to 300"),
    ({"text": "   "}, "1 to 300"),
    ({"text": "/config"}, "must not start with /"),
    ({"text": "!/clear"}, "must not start with /"),
    ({"text": "ok", "domain": "Bad Domain"}, "invalid domain"),
    ({"text": "ok", "extra": 1}, "invalid delegation request"),
])
def test_bad_input_is_denied_without_asking(broker, data, message):
    verdict = approve(broker, data)
    assert not verdict.allow and message in verdict.message
    assert broker.channel.calls == []


def test_helper_token_cannot_delegate(broker):
    scope = broker.open_scope("task-bbbbbb", task_id="bbbbbb")
    scope.begin_turn()
    result = call(broker, {"text": "x"}, token=scope.token)
    assert result["isError"] and "delegation is off" in result["content"][0]["text"]
    tool_use_id = f"toolu_{time.monotonic_ns()}"
    scope.observe({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tool_use_id, "name": DELEGATE_TOOL, "input": {"text": "x"}}]}})
    verdict = ask_broker(str(broker.socket_path), scope.token, DELEGATE_TOOL, {"text": "x"}, tool_use_id, 5)
    assert verdict["behavior"] == "deny" and "delegation is off" in verdict["message"]


def test_note_context_raises_delegation_to_level_three(broker):
    broker.set_turn_context("note context in this turn")
    assert approve(broker, {"text": "from a note"}).allow
    assert broker.channel.calls[0].level == Level.DANGER


def test_delegation_off_offers_nothing(tmp_path):
    b = ApprovalBroker(tmp_path, channel=Channel(), bind_wait_s=0.3)
    path = b.start(tmp_path / "run")
    try:
        env = json.loads(path.read_text())["mcpServers"]["pa"]["env"]
        assert ENV_DELEGATE not in env
        b.begin_turn()
        tool_use_id = f"toolu_{time.monotonic_ns()}"
        b.observe({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": tool_use_id, "name": DELEGATE_TOOL, "input": {"text": "x"}}]}})
        assert b.decide(DELEGATE_TOOL, {"text": "x"}, tool_use_id).message == "delegation is off"
    finally:
        b.close()
    config = BridgeConfig(workdir=tmp_path, config_dir=tmp_path, tools=("Read",), permission_tool="mcp__pa__approve",
                          expected_mcp=("pa",))
    init = {"type": "system", "subtype": "init", "tools": ["Read", "mcp__pa__approve", DELEGATE_TOOL],
            "mcp_servers": [{"name": "pa"}], "plugins": []}
    assert "unexpected tool" in init_problem(init, config)
    with_delegation = BridgeConfig(workdir=tmp_path, config_dir=tmp_path, tools=("Read",),
                                   permission_tool="mcp__pa__approve", expected_mcp=("pa",),
                                   extra_tools=(DELEGATE_TOOL,))
    assert init_problem(init, with_delegation) is None


# --- chat end to end ---------------------------------------------------------------------


@pytest.fixture
def chat(tmp_path, monkeypatch):
    log = tmp_path / "fake.jsonl"
    wrapper = tmp_path / "claude"
    wrapper.write_text(
        f"#!/bin/sh\nFAKE_CLAUDE_LOG={shlex.quote(str(log))} exec "
        f"{shlex.quote(sys.executable)} {shlex.quote(str(FAKE))} \"$@\"\n"
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "state"
    monkeypatch.setenv("PA_STATE_DIR", str(state))
    monkeypatch.setenv("PA_CLAUDE_BIN", str(wrapper))
    monkeypatch.setenv("PA_TASK_DELEGATION", "on")
    monkeypatch.setenv("PA_CORE_PLUGIN_DIR", "off")
    for name in ("PA_WORKDIR", "PA_VAULT_DIR", "PA_CLAUDE_CONFIG_DIR", "PA_STATUS_DIR", "PA_TASKS",
                 "PA_TASK_PARALLEL", "PA_TASK_DISCORD", "PA_DISCORD_WEBHOOK", "PA_QUOTA_STOP",
                 "PA_TASK_QUOTA_STOP", "PA_ROUTER_SMALL_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(approval, "stdin_is_tty", lambda: True)
    monkeypatch.setattr(approval, "discard_typeahead", lambda: True)

    def run(lines, answer):
        from personal_agent.cli import main

        inputs = iter(lines)
        monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
        monkeypatch.setattr(approval, "read_stdin_line", lambda timeout: answer)
        return main(["chat"])

    def tasks():
        path = state / "tasks.json"
        return json.loads(path.read_text())["tasks"] if path.exists() else []

    def starts():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return SimpleNamespace(run=run, tasks=tasks, starts=starts, state=state)


def test_chat_delegation_approved(chat, capsys):
    assert chat.run(["DELEGATE - write a summary", "/exit"], "y") == 0
    out = capsys.readouterr().out
    assert f"[approval · chat] level 2 (write): {DELEGATE_TOOL}" in out
    [task] = chat.tasks()
    assert task["origin"] == "assistant" and task["text"] == "write a summary"
    assert f"agent[3]> delegated {task['id']}" in out
    main_args = chat.starts()[0]["args"]
    assert "Skill" in main_args[main_args.index("--tools") + 1]


def test_chat_delegation_denied(chat, capsys):
    assert chat.run(["DELEGATE - write a summary", "/exit"], "n") == 0
    out = capsys.readouterr().out
    assert "agent[3]> denied" in out and chat.tasks() == []
