"""Module registry and PA_MODULES gating."""

import pytest

from personal_agent.cli import main
from personal_agent.config import Config, ConfigError
from personal_agent.modules import KINDS, MODULES, NAMES, by_name, describe, parse_modules


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PA_STATE_DIR", str(tmp_path / "state"))
    for name in ("PA_MODULES", "PA_BRIEF_TIME", "PA_TASKS", "PA_TELEGRAM_TOKEN", "PA_ACCESS_MODE",
                 "PA_DOMAIN_CONTEXT_CHARS", "PA_NOTIFY_BRIEF", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_manifests_are_consistent():
    assert len(set(NAMES)) == len(NAMES)
    for module in MODULES:
        assert module.kind in KINDS and module.config_prefix.startswith("PA_")
        assert by_name(module.name) is module
    assert by_name("nope") is None


@pytest.mark.parametrize("raw,expected", [
    (None, NAMES), ("", NAMES), ("all", NAMES), ("none", ()),
    ("telegram, BRIEF", ("brief", "telegram")), ("tasks,tasks", ("tasks",)),
])
def test_parse_modules(raw, expected):
    assert parse_modules(raw) == expected


def test_unknown_module_is_an_error(env):
    env.setenv("PA_MODULES", "brief,mail")
    with pytest.raises(ConfigError, match="unknown module.*mail"):
        Config.from_env()


def test_switched_off_modules_override_their_settings(env):
    env.setenv("PA_BRIEF_TIME", "07:30")
    env.setenv("PA_TASKS", "on")
    env.setenv("PA_DOMAIN_CONTEXT_CHARS", "2000")
    assert Config.from_env().brief_time == (7, 30)
    env.setenv("PA_MODULES", "telegram")
    config = Config.from_env()
    assert config.modules == ("telegram",)
    assert config.brief_time is None and not config.tasks_enabled and config.domain_context_chars == 0


def test_telegram_route_needs_the_telegram_module(env):
    env.setenv("PA_MODULES", "brief")
    env.setenv("PA_NOTIFY_BRIEF", "telegram")
    with pytest.raises(ConfigError):
        Config.from_env()


def test_describe_marks_on_and_off():
    lines = describe(("brief",))
    assert any(line.startswith("on ") and "brief" in line for line in lines)
    assert any(line.startswith("off") and "tasks" in line for line in lines)


def test_modules_command(env, capsys):
    env.setenv("PA_MODULES", "brief")
    assert main(["modules"]) == 0
    out = capsys.readouterr().out
    assert "access mode: subscription" in out and "on   brief" in out and "off  telegram" in out
