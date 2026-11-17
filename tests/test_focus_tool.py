"""F1b: the assistant's focus tool (Marco 17.11.2026: the assistant adds and removes items too)."""

import json
import time

import pytest

from personal_agent.approval import ApprovalBroker, Level, classify
from personal_agent.approval_server import (
    ENV_FOCUS, FOCUS_TOOL_NAME, SERVER_NAME, ask_broker, ask_broker_focus, handle,
)
from personal_agent.audit import read_audit
from personal_agent.bridge import FOCUS_TOOL
from personal_agent.focus import AGENT_SOURCE, OWNER_ID, SHARED, FocusService, FocusStore, Person, tool_command
from personal_agent.roles import Role


class Channel:
    def __init__(self, answer=True):
        self.answer = answer
        self.calls = []

    def ask(self, request, deadline):
        self.calls.append(request)
        return self.answer


@pytest.fixture
def env(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    store = FocusStore(state)
    broker = ApprovalBroker(work, channel=Channel(), bind_wait_s=0.3)
    broker.audit_dir = state
    broker.enable_focus(FocusService(store, Person(OWNER_ID), "en"))
    broker.start(tmp_path / "run")
    broker.begin_turn()
    yield broker, store, state
    broker.close()


def run(broker, tool_input, token=None):
    """Announce the call, let the permission tool decide, then make the call: like Claude Code."""
    tool_use_id = f"toolu_{time.monotonic_ns()}"
    broker.observe({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tool_use_id, "name": FOCUS_TOOL, "input": tool_input}]}})
    verdict = broker.decide(FOCUS_TOOL, tool_input, tool_use_id)
    result = ask_broker_focus(str(broker.socket_path), token or broker.token, tool_input, 5)
    return verdict, result


def text(result):
    return result["content"][0]["text"]


def test_names_and_level():
    assert FOCUS_TOOL == f"mcp__{SERVER_NAME}__{FOCUS_TOOL_NAME}"
    assert classify(FOCUS_TOOL, {"op": "show"}) == Level.WRITE


def test_tool_is_offered_only_with_the_flag(env):
    listed = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, {})
    assert [t["name"] for t in listed["result"]["tools"]] == ["approve"]
    listed = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, {ENV_FOCUS: "1"})
    assert [t["name"] for t in listed["result"]["tools"]] == ["approve", "focus"]
    refused = handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "focus", "arguments": {"op": "show"}}}, {})
    assert refused["error"]["message"] == "unknown tool"
    broker = env[0]
    assert json.loads(broker.mcp_config_path.read_text())["mcpServers"]["pa"]["env"][ENV_FOCUS] == "1"
    scope = broker.open_scope("task-aaaaaa", task_id="aaaaaa")
    assert ENV_FOCUS not in json.loads(scope.mcp_config_path.read_text())["mcpServers"]["pa"]["env"]


def test_assistant_adds_and_removes_on_own_lists_without_a_prompt(env):
    broker, store, state = env
    verdict, result = run(broker, {"op": "add", "list": "focus", "text": "Angebot schreiben"})
    assert verdict.allow and text(result) == "added #1 to Focus" and broker.channel.calls == []
    item = store.items(Person(OWNER_ID), "focus")[0]
    assert item.created_by == OWNER_ID  # the assistant acts for the person
    conn_source = __import__("sqlite3").connect(store.path).execute("SELECT source FROM items").fetchone()[0]
    assert conn_source == AGENT_SOURCE
    verdict, result = run(broker, {"op": "drop", "item_id": 1})
    assert verdict.allow and text(result) == "#1 dropped: Angebot schreiben"
    verdict, result = run(broker, {"op": "show", "list": "focus"})
    assert "(empty)" in text(result)
    # every change is in the audit log, with the reason it ran without a prompt
    reasons = [e["reason"] for e in read_audit(state) if e.get("tool") == FOCUS_TOOL]
    assert reasons.count("own or shared list, no prompt needed") == 2


def test_change_to_another_persons_list_asks_first(env):
    broker, store, _ = env
    other, _ = store.add(Person("partner", Role.OWNER), "partner's item", "focus")
    broker.channel.answer = False
    verdict, result = run(broker, {"op": "done", "item_id": other.id})
    assert not verdict.allow and len(broker.channel.calls) == 1
    assert broker.channel.calls[0].level == Level.WRITE and "change the focus lists" in broker.channel.calls[0].summary
    assert result["isError"] and store.items(Person("partner", Role.OWNER), "focus")[0].status == "open"
    broker.channel.answer = True
    verdict, result = run(broker, {"op": "done", "item_id": other.id})
    assert verdict.allow and text(result).startswith(f"#{other.id} done")


def test_private_item_of_someone_else_stays_invisible(env):
    broker, store, _ = env
    secret, _ = store.add(Person("partner", Role.OWNER), "surprise", "shopping", private=True)
    verdict, result = run(broker, {"op": "drop", "item_id": secret.id})  # asks, the person says yes ...
    assert verdict.allow and result["isError"] and f"no item #{secret.id}" in text(result)  # ... still refused
    assert "surprise" not in text(run(broker, {"op": "show", "list": "shopping"})[1])


def test_note_context_makes_every_change_ask(env):
    broker, _, _ = env
    broker.set_turn_context("note context in this turn")
    broker.channel.answer = False
    verdict, result = run(broker, {"op": "add", "list": "inbox", "text": "from a note"})
    assert not verdict.allow and len(broker.channel.calls) == 1 and result["isError"]
    verdict, _ = run(broker, {"op": "show"})  # reading still needs no prompt
    assert verdict.allow and len(broker.channel.calls) == 1


def test_call_without_approval_is_refused_and_one_shot(env):
    broker, store, _ = env
    result = ask_broker_focus(str(broker.socket_path), broker.token, {"op": "add", "list": "inbox", "text": "x"}, 5)
    assert result["isError"] and "not approved" in text(result)
    data = {"op": "add", "list": "inbox", "text": "once"}
    verdict, result = run(broker, data)
    assert verdict.allow and not result.get("isError")
    replay = ask_broker_focus(str(broker.socket_path), broker.token, data, 5)
    assert replay["isError"] and len(store.items(Person(OWNER_ID), "inbox")) == 1


def test_approval_does_not_carry_over_to_the_next_turn(env):
    broker, store, _ = env
    data = {"op": "add", "list": "inbox", "text": "later"}
    tool_use_id = "toolu_x1"
    broker.observe({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tool_use_id, "name": FOCUS_TOOL, "input": data}]}})
    assert broker.decide(FOCUS_TOOL, data, tool_use_id).allow
    broker.begin_turn()
    assert ask_broker_focus(str(broker.socket_path), broker.token, data, 5)["isError"]
    assert store.items(Person(OWNER_ID), "inbox") == []


def test_helper_scope_has_no_focus(env):
    broker, _, _ = env
    scope = broker.open_scope("task-bbbbbb", task_id="bbbbbb")
    scope.begin_turn()
    tool_use_id = f"toolu_{time.monotonic_ns()}"
    scope.observe({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tool_use_id, "name": FOCUS_TOOL, "input": {"op": "show"}}]}})
    verdict = ask_broker(str(broker.socket_path), scope.token, FOCUS_TOOL, {"op": "show"}, tool_use_id, 5)
    assert verdict["behavior"] == "deny" and "focus lists are off" in verdict["message"]
    result = ask_broker_focus(str(broker.socket_path), scope.token, {"op": "show"}, 5)
    assert result["isError"] and "focus lists are off" in text(result)


@pytest.mark.parametrize("data, message", [
    ({"op": "add", "list": "focus"}, "add needs"),
    ({"op": "add", "list": "focus", "text": "x" * 301}, "add needs"),
    ({"op": "add", "list": "focus", "text": "x", "shared": True, "private": True}, "add needs"),
    ({"op": "add", "list": "nope", "text": "x"}, "unknown list"),
    ({"op": "done"}, "done needs item_id"),
    ({"op": "done", "item_id": True}, "invalid focus request"),
    ({"op": "move", "item_id": 1}, "move needs list"),
    ({"op": "delete", "item_id": 1}, "unknown op"),
    ({"op": "show", "owner": "partner"}, "invalid focus request"),
])
def test_bad_input_is_denied_without_asking(env, data, message):
    broker, _, _ = env
    verdict, _ = run(broker, data)
    assert not verdict.allow and message in verdict.message and broker.channel.calls == []


def test_tool_command_maps_shared_add():
    command, problem = tool_command({"op": "add", "list": "focus", "text": "Steuer", "shared": True})
    assert problem is None and command.shared and command.list == "focus"
    assert SHARED == "shared"
