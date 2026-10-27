"""Access mode: exactly one way to reach Claude per installation (subscription or API key)."""

from pathlib import Path

import pytest

from personal_agent.bridge import NO_TELEMETRY_ENV, BridgeConfig, BridgeError, TurnResult, build_env
from personal_agent.config import Config, ConfigError
from personal_agent.quota import QuotaGuard
from personal_agent.router import PaidUsageStop, SafetyStop, guarded_send

KEY = "sk-ant-test-key-value"  # pragma: allowlist secret
TOKEN = "oauth-test-token"  # pragma: allowlist secret


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PA_STATE_DIR", str(tmp_path / "state"))
    for name in ("PA_ACCESS_MODE", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


# --- configuration ----------------------------------------------------------------------------


def test_default_is_subscription_and_ignores_an_inherited_api_key(env):
    env.setenv("ANTHROPIC_API_KEY", KEY)
    config = Config.from_env()
    assert config.access_mode == "subscription" and config.claude_api_key is None


def test_api_mode_reads_the_key_and_hides_it(env):
    env.setenv("PA_ACCESS_MODE", "API")
    env.setenv("ANTHROPIC_API_KEY", KEY)
    config = Config.from_env()
    assert config.access_mode == "api" and config.claude_api_key == KEY
    assert KEY not in repr(config)


def test_api_mode_without_key_is_an_error(env):
    env.setenv("PA_ACCESS_MODE", "api")
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        Config.from_env()


def test_api_mode_with_oauth_token_is_an_error(env):
    env.setenv("PA_ACCESS_MODE", "api")
    env.setenv("ANTHROPIC_API_KEY", KEY)
    env.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    with pytest.raises(ConfigError, match="exactly one access path") as exc:
        Config.from_env()
    assert KEY not in str(exc.value) and TOKEN not in str(exc.value)


def test_unknown_mode_is_an_error(env):
    env.setenv("PA_ACCESS_MODE", "both")
    with pytest.raises(ConfigError, match="PA_ACCESS_MODE"):
        Config.from_env()


# --- child environment ------------------------------------------------------------------------


def bridge(**kw):
    return BridgeConfig(workdir=Path("/w"), config_dir=Path("/c"), **kw)


def test_api_mode_passes_only_the_api_key():
    base = {"PATH": "/bin", "ANTHROPIC_API_KEY": "inherited", "CLAUDE_CODE_OAUTH_TOKEN": "inherited"}  # pragma: allowlist secret
    env = build_env(bridge(api_key=KEY), base=base)
    assert env["ANTHROPIC_API_KEY"] == KEY and "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_subscription_mode_never_passes_an_api_key():
    base = {"PATH": "/bin", "ANTHROPIC_API_KEY": "inherited"}  # pragma: allowlist secret
    env = build_env(bridge(oauth_token=TOKEN), base=base)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN and "ANTHROPIC_API_KEY" not in env


def test_both_credentials_are_refused():
    with pytest.raises(BridgeError, match="exactly one access path"):
        build_env(bridge(api_key=KEY, oauth_token=TOKEN), base={})


@pytest.mark.parametrize("kw", [{}, {"api_key": KEY}, {"oauth_token": TOKEN}])
def test_telemetry_is_off_in_every_mode(kw):
    env = build_env(bridge(**kw), base={})
    assert all(env[name] == value for name, value in NO_TELEMETRY_ENV.items())


def test_bridge_config_repr_hides_the_api_key():
    assert KEY not in repr(bridge(api_key=KEY))


# --- tripwire in guarded_send -----------------------------------------------------------------


class Session:
    def __init__(self, config, events):
        self.config, self.events = config, events

    def send(self, text, on_event=None):
        for event in self.events:
            on_event(event)
        return TurnResult("ok", "sid", False, "success", [], 0.0, None)


def init(source):
    return {"type": "system", "subtype": "init", "apiKeySource": source}


def guard(tmp_path):
    return QuotaGuard(stop_at=0.9, state_file=tmp_path / "quota.json")


def test_api_session_with_api_key_source_passes(tmp_path):
    result = guarded_send(Session(bridge(api_key=KEY), [init("ANTHROPIC_API_KEY")]), "hi", guard(tmp_path))
    assert result.text == "ok"


@pytest.mark.parametrize("source", ["none", "/login managed key", None])
def test_api_session_with_another_access_path_stops(tmp_path, source):
    with pytest.raises(SafetyStop, match="instead of the configured API key") as exc:
        guarded_send(Session(bridge(api_key=KEY), [init(source)]), "hi", guard(tmp_path))
    assert not isinstance(exc.value, PaidUsageStop)


def test_api_session_ignores_overage_reports(tmp_path):
    event = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "isUsingOverage": True}}
    result = guarded_send(Session(bridge(api_key=KEY), [event]), "hi", guard(tmp_path))
    assert result.text == "ok"


def test_subscription_session_still_stops_on_an_api_key_source(tmp_path):
    with pytest.raises(PaidUsageStop):
        guarded_send(Session(bridge(), [init("ANTHROPIC_API_KEY")]), "hi", guard(tmp_path))
