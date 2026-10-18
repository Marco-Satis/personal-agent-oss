import json
import os
import stat
import sys
from pathlib import Path

import pytest

from personal_agent.bridge import (
    HARDENED_SETTINGS,
    BridgeConfig,
    BridgeError,
    ClaudeSession,
    build_args,
    build_env,
    user_message,
)

FAKE = Path(__file__).resolve().parent / "fake_claude.py"


@pytest.fixture
def fake_bin(tmp_path):
    """Executable wrapper so the bridge can start the fake like the real CLI."""
    wrapper = tmp_path / "claude"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return wrapper


def make_config(tmp_path, fake_bin, **kw):
    return BridgeConfig(workdir=tmp_path / "work", config_dir=tmp_path / "cfg", claude_bin=str(fake_bin), **kw)


def read_log(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


# --- arguments and environment ------------------------------------------------


def test_args_are_locked_down(tmp_path):
    args = build_args(BridgeConfig(workdir=tmp_path, config_dir=tmp_path))
    for flag in ("--restricted", "--strict-mcp-config", "--verbose"):
        assert flag in args
    assert args[args.index("--tools") + 1] == "Read,Glob,Grep"
    assert args[args.index("--input-format") + 1] == "stream-json"
    assert "--resume" not in args
    assert "--dangerously-skip-permissions" not in args


def test_args_optional_parts(tmp_path):
    cfg = BridgeConfig(
        workdir=tmp_path,
        config_dir=tmp_path,
        mcp_config=tmp_path / "mcp.json",
        permission_tool="mcp__pa__approve",
        plugin_dirs=(tmp_path / "p1", tmp_path / "p2"),
    )
    args = build_args(cfg, resume="abc")
    assert args[args.index("--permission-prompt-tool") + 1] == "mcp__pa__approve"
    assert args.count("--plugin-dir") == 2
    assert args[args.index("--resume") + 1] == "abc"


def test_args_always_disable_hooks_and_ask_for_skills(tmp_path):
    args = build_args(BridgeConfig(workdir=tmp_path, config_dir=tmp_path, tools=()))
    index = args.index("--strict-mcp-config")
    assert args[index + 1] == "--settings"
    settings = json.loads(args[index + 2])
    assert args[index + 2] == HARDENED_SETTINGS
    assert settings == {"disableAllHooks": True, "permissions": {"ask": ["Skill"]}}
    assert args[args.index("--tools") + 1] == ""


def test_one_plugin_dir_flag_per_plugin_in_order(tmp_path):
    dirs = (tmp_path / "core", tmp_path / "b", tmp_path / "a")
    args = build_args(BridgeConfig(workdir=tmp_path, config_dir=tmp_path, plugin_dirs=dirs))
    values = [args[i + 1] for i, arg in enumerate(args) if arg == "--plugin-dir"]
    assert values == [str(d) for d in dirs]


def test_chat_tools_include_skill():
    from personal_agent.cli import CHAT_TOOLS

    assert "Skill" in CHAT_TOOLS


def test_env_never_passes_api_keys(tmp_path):
    base = {"PATH": "/bin", "HOME": "/h", "ANTHROPIC_API_KEY": "x", "OPENAI_API_KEY": "y", "SECRET": "z"}
    env = build_env(BridgeConfig(workdir=tmp_path, config_dir=tmp_path / "c", oauth_token="tok"), base=base)
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "c")
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"
    assert "ANTHROPIC_API_KEY" not in env and "OPENAI_API_KEY" not in env and "SECRET" not in env


def test_token_not_in_repr(tmp_path):
    assert "s3cr3t-v4lue" not in repr(BridgeConfig(workdir=tmp_path, config_dir=tmp_path, oauth_token="s3cr3t-v4lue"))


def test_user_message_validation():
    assert json.loads(user_message(" hi "))["message"]["content"] == "hi"
    with pytest.raises(BridgeError):
        user_message("   ")
    with pytest.raises(BridgeError):
        user_message("x" * 20_001)


# --- conversation with the fake CLI ---------------------------------------------


def test_turns_share_one_process_and_session(tmp_path, fake_bin, monkeypatch):
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    session = ClaudeSession(make_config(tmp_path, fake_bin), extra_env={"FAKE_CLAUDE_LOG": str(log)})
    events = []
    try:
        first = session.send("hello", on_event=events.append)
        second = session.send("again")
    finally:
        session.close()
    assert first.text == "echo: hello" and not first.is_error
    assert second.text == "echo: again"
    assert first.session_id and first.session_id == second.session_id
    assert first.rate_limit["unifiedWindows"]["five_hour"]["utilization"] == 0.25
    assert [e["type"] for e in events][0] == "system"
    entries = read_log(log)
    assert len(entries) == 1  # one process for both turns
    assert entries[0]["cwd"] == str(tmp_path / "work")
    assert "ANTHROPIC_API_KEY" not in entries[0]["env"]


def test_error_result_is_reported(tmp_path, fake_bin):
    session = ClaudeSession(make_config(tmp_path, fake_bin))
    try:
        result = session.send("FAIL")
    finally:
        session.close()
    assert result.is_error and result.subtype == "error_during_execution"


def test_crash_then_resume_with_same_session(tmp_path, fake_bin):
    log = tmp_path / "log.jsonl"
    session = ClaudeSession(make_config(tmp_path, fake_bin), extra_env={"FAKE_CLAUDE_LOG": str(log)})
    try:
        first = session.send("hello")
        with pytest.raises(BridgeError):
            session.send("DIE")
        assert not session.running
        again = session.send("back")
    finally:
        session.close()
    assert again.session_id == first.session_id
    second_start = read_log(log)[1]["args"]
    assert second_start[second_start.index("--resume") + 1] == first.session_id


def test_timeout_stops_process(tmp_path, fake_bin):
    session = ClaudeSession(make_config(tmp_path, fake_bin), turn_timeout_s=1)
    try:
        with pytest.raises(BridgeError):
            session.send("SLOW")
        assert not session.running
    finally:
        session.close()


def test_missing_binary(tmp_path):
    session = ClaudeSession(BridgeConfig(workdir=tmp_path / "w", config_dir=tmp_path / "c", claude_bin=str(tmp_path / "nope")))
    with pytest.raises(BridgeError):
        session.send("hi")


def test_chat_cli_with_fake(tmp_path, fake_bin, monkeypatch, capsys):
    from personal_agent.cli import main

    monkeypatch.setenv("PA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PA_CLAUDE_BIN", str(fake_bin))
    inputs = iter(["hello", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    assert main(["chat"]) == 0
    out = capsys.readouterr().out
    assert "agent[3]> echo: hello" in out
    assert os.path.isdir(tmp_path / "state" / "vault")
