import json
import shlex
import stat
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from personal_agent import approval
from personal_agent.approval import ApprovalBroker, ConsoleChannel, Level
from personal_agent.cli import main
from personal_agent.config import Config, ConfigError

FAKE = Path(__file__).resolve().parent / "fake_claude.py"
ENV_NAMES = ("PA_WORKDIR", "PA_VAULT_DIR", "PA_VAULT_CONTEXT_CHARS", "PA_CLAUDE_CONFIG_DIR", "PA_STATUS_DIR",
             "PA_QUOTA_STOP", "PA_ROUTER_SMALL_MODEL", "PA_CLAUDE_MODEL", "PA_LANGUAGE", "PA_DISCORD_WEBHOOK",
             "PA_PUBLIC_APPROVAL_URL", "PA_BRIEF_TIME", "PA_BRIEF_GRACE_MIN", "PA_BRIEF_POLISH", "PA_BRIEF_DISCORD",
             "PA_SESSIONS_SOURCE", "PA_TASKS", "PA_TASK_PARALLEL", "PA_TASK_MODEL", "PA_TASK_TIMEOUT_S",
             "PA_TASK_QUOTA_STOP", "PA_TASK_DISCORD", "PA_TASK_DELEGATION", "PA_TASK_MAX_AGE_H",
             "PA_DOMAIN_CONTEXT_CHARS")


@pytest.fixture
def env(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("PA_STATE_DIR", str(state))
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    return SimpleNamespace(state=state, tmp=tmp_path)


# --- configuration --------------------------------------------------------------


def test_vault_path_default_and_legacy_alias(env, monkeypatch):
    config = Config.from_env()
    assert config.vault_path == env.state / "vault" and config.vault_context_chars == 3000
    assert config.bridge_paths()[0] == env.state / "vault"
    monkeypatch.setenv("PA_WORKDIR", str(env.tmp / "legacy"))
    assert Config.from_env().vault_path == env.tmp / "legacy"
    monkeypatch.setenv("PA_VAULT_DIR", str(env.tmp / "legacy"))  # same path: fine
    assert Config.from_env().vault_path == env.tmp / "legacy"
    monkeypatch.setenv("PA_VAULT_DIR", str(env.tmp / "other"))
    with pytest.raises(ConfigError, match="not both"):
        Config.from_env()


def test_vault_context_chars_range(env, monkeypatch):
    for value, expected in (("0", 0), ("12000", 12000), ("500", 500)):
        monkeypatch.setenv("PA_VAULT_CONTEXT_CHARS", value)
        assert Config.from_env().vault_context_chars == expected
    for bad in ("-1", "12001", "lots"):
        monkeypatch.setenv("PA_VAULT_CONTEXT_CHARS", bad)
        with pytest.raises(ConfigError):
            Config.from_env()


# --- vault init / path ------------------------------------------------------------


def test_vault_init_output_and_idempotence(env, capsys):
    assert main(["vault", "init"]) == 0
    out = capsys.readouterr().out
    for name in ("raw/", "wiki/", "outputs/", "CLAUDE.md", "heute.md", "wiki/_master-index.md"):
        assert f"created {name}" in out
    assert (env.state / "vault" / "heute.md").exists()
    assert main(["vault", "init"]) == 0
    out = capsys.readouterr().out
    assert "created" not in out and "kept heute.md" in out
    assert main(["vault", "path"]) == 0
    assert capsys.readouterr().out.strip() == str(env.state / "vault")


def test_vault_init_reports_skipped_symlink(env, capsys):
    root = env.state / "vault"
    root.mkdir(parents=True)
    (root / "heute.md").symlink_to(env.tmp / "elsewhere.md")
    assert main(["vault", "init"]) == 0
    assert "skipped heute.md (symlink or not a regular file)" in capsys.readouterr().out
    assert not (env.tmp / "elsewhere.md").exists()


@pytest.mark.parametrize("where, message", [
    ("contains_state", "workdir must not contain"),
    ("run", "vault must not be inside the broker runtime directory"),
    ("run_child", "vault must not be inside the broker runtime directory"),
    ("plugin", "vault must not be inside the plugins directory"),
    ("config", "vault must not be inside the Claude Code config directory"),
    ("status", "workdir must not be inside the session status directory"),
])
def test_vault_init_refuses_unsafe_locations(env, monkeypatch, capsys, where, message):
    vault = {
        "contains_state": env.tmp,
        "run": env.state / "run",
        "run_child": env.state / "run" / "x",
        "plugin": env.state / "plugins" / "x",
        "config": env.state / "claude-config" / "v",
        "status": env.state / "sessions" / "v",
    }[where]
    monkeypatch.setenv("PA_VAULT_DIR", str(vault))
    assert main(["vault", "init"]) == 2
    assert message in capsys.readouterr().err
    assert not (vault / "heute.md").exists()


def test_vault_must_not_contain_state_files(env, monkeypatch, capsys):
    monkeypatch.setenv("PA_STATE_DIR", str(env.tmp / "v" / "state"))
    monkeypatch.setenv("PA_VAULT_DIR", str(env.tmp / "v"))
    assert main(["vault", "init"]) == 2
    assert "workdir must not contain" in capsys.readouterr().err


def test_vault_dir_conflict_is_a_config_error(env, monkeypatch, capsys):
    monkeypatch.setenv("PA_VAULT_DIR", str(env.tmp / "a"))
    monkeypatch.setenv("PA_WORKDIR", str(env.tmp / "b"))
    assert main(["vault", "path"]) == 2
    assert "not both" in capsys.readouterr().err


# --- brief ---------------------------------------------------------------------------


def test_brief_cli_writes_the_file(env, capsys):
    assert main(["vault", "init"]) == 0
    (env.state / "vault" / "wiki" / "todo.md").write_text("- [ ] renew the passport\n")
    capsys.readouterr()
    assert main(["brief", "--quiet"]) == 0
    assert "# Brief" not in capsys.readouterr().out
    [written] = list((env.state / "vault" / "outputs").glob("brief-*.md"))
    assert "- [ ] renew the passport — wiki/todo.md" in written.read_text()
    assert main(["brief"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Brief ") and "renew the passport" in out
    assert json.loads((env.state / "brief.json").read_text())["status"] is None


def test_brief_cli_without_vault_and_send_without_webhook(env, capsys):
    assert main(["brief"]) == 0
    assert "(no vault)" in capsys.readouterr().out
    assert main(["brief", "--send"]) == 2
    assert "--send needs PA_DISCORD_WEBHOOK" in capsys.readouterr().err


# --- chat with the vault ------------------------------------------------------------


@pytest.fixture
def chat(env, monkeypatch):
    log = env.tmp / "fake.jsonl"
    wrapper = env.tmp / "claude"
    wrapper.write_text(
        f"#!/bin/sh\nFAKE_CLAUDE_LOG={shlex.quote(str(log))} exec "
        f"{shlex.quote(sys.executable)} {shlex.quote(str(FAKE))} \"$@\"\n"
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PA_CLAUDE_BIN", str(wrapper))

    def run(lines):
        inputs = iter(lines)
        monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
        return main(["chat"])

    def starts():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return SimpleNamespace(run=run, starts=starts, state=env.state, vault=env.state / "vault")


def test_chat_creates_vault_and_notes_without_a_process(chat, capsys):
    assert chat.run(["note call the dentist", "/exit"]) == 0
    out = capsys.readouterr().out
    assert "[vault: created raw/, wiki/, outputs/, CLAUDE.md, heute.md, wiki/_master-index.md]" in out
    assert "agent[1]> noted in raw/inbox-" in out
    [inbox] = list((chat.vault / "raw").iterdir())
    assert inbox.read_text().rstrip().endswith("call the dentist")
    assert chat.starts() == []


def test_chat_attaches_excerpts_and_system_prompt(chat, capsys):
    (chat.vault / "wiki").mkdir(parents=True)
    (chat.vault / "wiki" / "dentist.md").write_text("# Dentist\n\nAppointment on Friday at 10.")
    assert chat.run(["hello", "Tell me about my dentist appointment", "/exit"]) == 0
    out = capsys.readouterr().out
    assert "[vault: created" in out  # the rest of the skeleton
    assert "agent[3]> echo: hello" in out
    assert "[context: " in out and 'echo: <vault-excerpts id="' in out
    assert "path=\"wiki/dentist.md\"" in out
    [start] = chat.starts()
    args = start["args"]
    assert start["cwd"] == str(chat.vault)
    prompt = Path(args[args.index("--append-system-prompt-file") + 1])
    assert prompt.parent.parent == chat.state / "run"
    assert not prompt.exists()  # removed with the runtime directory
    assert list((chat.state / "run").iterdir()) == []


# --- broker runtime file and approval context -----------------------------------------


def test_runtime_file_is_private_and_removed(tmp_path):
    broker = ApprovalBroker(tmp_path / "work")
    broker.start(tmp_path / "run")
    path = broker.write_runtime_file("vault-system-prompt.md", "rules")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and path.read_text() == "rules"
    for bad in ("../x", "s", "mcp.json", ".hidden"):
        with pytest.raises(ValueError):
            broker.write_runtime_file(bad, "x")
    runtime = broker.runtime_dir
    broker.close()
    assert not runtime.exists()


def test_turn_context_marks_approval_requests(tmp_path, monkeypatch):
    seen = []

    class Channel:
        def ask(self, request, deadline):
            seen.append(request)
            return True

    work = tmp_path / "work"
    work.mkdir()
    broker = ApprovalBroker(work, channel=Channel(), bind_wait_s=0.2)
    broker.begin_turn()
    broker.set_turn_context("note context in this turn")

    def announce_and_decide(tool_use_id):
        tool_input = {"file_path": "a.md", "content": "x"}
        broker.observe({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": tool_use_id, "name": "Write", "input": tool_input}]}})
        return broker.decide("Write", tool_input, tool_use_id)

    assert announce_and_decide("toolu_1").allow
    assert seen[-1].context == "note context in this turn"
    broker.begin_turn()  # a new turn forgets the marker
    assert announce_and_decide("toolu_2").allow
    assert seen[-1].context is None

    lines = []
    monkeypatch.setattr(approval, "stdin_is_tty", lambda: True)
    monkeypatch.setattr(approval, "discard_typeahead", lambda: True)
    channel = ConsoleChannel(threading.Lock(), read_line=lambda timeout: "y", write=lines.append)
    request = approval.ApprovalRequest("Write", Level.WRITE, "a.md", "t", context="note context in this turn")
    assert channel.ask(request, deadline=time.monotonic() + 5)
    assert any("may come from note text or a plugin skill" in line for line in lines)


# --- phase 3: task state files and configuration ------------------------------------------

TASK_ENV = ("PA_TASKS", "PA_TASK_PARALLEL", "PA_TASK_MODEL", "PA_TASK_TIMEOUT_S", "PA_TASK_QUOTA_STOP",
            "PA_TASK_DISCORD", "PA_TASK_DELEGATION", "PA_TASK_MAX_AGE_H", "PA_DOMAIN_CONTEXT_CHARS")


def test_vault_must_not_contain_the_task_board(env, monkeypatch, capsys):
    from personal_agent.cli import STATE_ENTRIES

    assert {"tasks.json", "tasks.lock", "tasks-owner.lock"} <= set(STATE_ENTRIES)
    vault = env.tmp / "v"
    vault.mkdir()
    monkeypatch.setenv("PA_STATE_DIR", str(vault))  # state files would land inside the vault
    monkeypatch.setenv("PA_VAULT_DIR", str(vault))
    assert main(["vault", "init"]) == 2
    assert "workdir must not contain" in capsys.readouterr().err


def test_task_config_defaults_and_values(env, monkeypatch):
    for name in TASK_ENV:
        monkeypatch.delenv(name, raising=False)
    config = Config.from_env()
    assert (config.tasks_enabled, config.task_parallel, config.task_model, config.task_timeout_s,
            config.task_quota_stop, config.task_discord, config.task_delegation, config.task_max_age_h) == (
        True, 2, None, 900, 0.8, "off", False, 24)
    monkeypatch.setenv("PA_QUOTA_STOP", "0.7")
    assert Config.from_env().task_quota_stop == 0.7  # never above the chat's threshold
    monkeypatch.setenv("PA_TASKS", "off")
    monkeypatch.setenv("PA_TASK_PARALLEL", "1")
    monkeypatch.setenv("PA_TASK_MODEL", "haiku")
    monkeypatch.setenv("PA_TASK_TIMEOUT_S", "60")
    monkeypatch.setenv("PA_TASK_QUOTA_STOP", "0.5")
    monkeypatch.setenv("PA_TASK_DISCORD", "counts")
    monkeypatch.setenv("PA_TASK_DELEGATION", "on")
    monkeypatch.setenv("PA_TASK_MAX_AGE_H", "48")
    config = Config.from_env()
    assert (config.tasks_enabled, config.task_parallel, config.task_model, config.task_timeout_s,
            config.task_quota_stop, config.task_discord, config.task_delegation, config.task_max_age_h) == (
        False, 1, "haiku", 60, 0.5, "counts", True, 48)


def test_domain_context_config(env, monkeypatch):
    monkeypatch.delenv("PA_DOMAIN_CONTEXT_CHARS", raising=False)
    assert Config.from_env().domain_context_chars == 4000
    for value in ("0", "8000"):
        monkeypatch.setenv("PA_DOMAIN_CONTEXT_CHARS", value)
        assert Config.from_env().domain_context_chars == int(value)


@pytest.mark.parametrize("name, value", [
    ("PA_TASKS", "maybe-secret"),
    ("PA_TASK_PARALLEL", "3"),
    ("PA_TASK_PARALLEL", "0"),
    ("PA_TASK_TIMEOUT_S", "59"),
    ("PA_TASK_TIMEOUT_S", "3601"),
    ("PA_TASK_QUOTA_STOP", "0.95"),
    ("PA_TASK_QUOTA_STOP", "-0.25"),
    ("PA_TASK_DISCORD", "full"),
    ("PA_TASK_DELEGATION", "sometimes"),
    ("PA_TASK_MAX_AGE_H", "9999"),
    ("PA_DOMAIN_CONTEXT_CHARS", "8001"),
    ("PA_DOMAIN_CONTEXT_CHARS", "-1"),
    ("PA_DOMAIN_CONTEXT_CHARS", "lots-secret"),
    ("PA_SESSIONS_SOURCE", "phone-secret"),
])
def test_task_config_errors_never_echo_the_value(env, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError) as info:
        Config.from_env()
    assert name in str(info.value) and value not in str(info.value)


def test_task_discord_without_webhook_warns_in_the_chat(chat, monkeypatch, capsys):
    monkeypatch.setenv("PA_TASK_DISCORD", "counts")
    assert chat.run(["/exit"]) == 0
    assert "PA_TASK_DISCORD=counts needs PA_DISCORD_WEBHOOK" in capsys.readouterr().out
