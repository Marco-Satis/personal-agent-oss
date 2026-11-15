"""ntfy as an opt-in notification channel (config, remote.NtfyClient, notify.NtfyTarget)."""

import io
import logging
from pathlib import Path

import pytest

from personal_agent import notify
from personal_agent.bridge import BridgeConfig, build_env
from personal_agent.config import Config, ConfigError
from personal_agent.notify import Notifier, NtfyTarget, info_notice, resolve_route, split_message
from personal_agent.remote import NtfyClient

URL = "https://push.example.org/agent-k3x9q2"
TOKEN = "tk_" + "A1" * 15  # synthetic, has the shape of an ntfy token  # pragma: allowlist secret


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("PA_STATE_DIR", str(tmp_path / "state"))
    for name in ("PA_NTFY_URL", "PA_NTFY_TOKEN", "PA_TELEGRAM_TOKEN", "PA_DISCORD_WEBHOOK",
                 *(f"PA_NOTIFY_{kind.upper()}" for kind in notify.KINDS)):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_config_accepts_own_server_and_hides_url_and_token(env):
    env.setenv("PA_NTFY_URL", URL + "/")
    env.setenv("PA_NTFY_TOKEN", TOKEN)
    env.setenv("PA_NOTIFY_BRIEF", "ntfy")
    env.setenv("PA_NOTIFY_ALERT", "ntfy")
    config = Config.from_env()
    assert config.ntfy_url == URL and config.ntfy_token == TOKEN
    assert config.route("brief") == ("ntfy",) and config.route("alert") == ("ntfy",)
    assert "k3x9q2" not in repr(config) and TOKEN not in repr(config)
    env.setenv("PA_NTFY_URL", "http://127.0.0.1:2586/agent")
    assert Config.from_env().ntfy_url == "http://127.0.0.1:2586/agent"  # plain http only on this machine


@pytest.mark.parametrize("bad", ["http://push.example.org/t", "https://push.example.org/", "https://push.example.org/a/b",
                                 "https://u@push.example.org/t", "https://push.example.org/t?x=1", "ftp://h/t"])
def test_bad_urls_are_rejected_without_echo(env, bad):
    env.setenv("PA_NTFY_URL", bad)
    with pytest.raises(ConfigError) as info:
        Config.from_env()
    assert "PA_NTFY_URL" in str(info.value) and bad not in str(info.value)


def test_token_shape_routes_and_no_approvals(env):
    env.setenv("PA_NTFY_TOKEN", TOKEN)
    with pytest.raises(ConfigError, match="PA_NTFY_TOKEN is set but PA_NTFY_URL is not"):
        Config.from_env()
    env.setenv("PA_NTFY_URL", URL)
    env.setenv("PA_NTFY_TOKEN", "tk_short-SSSSSS")
    with pytest.raises(ConfigError) as info:
        Config.from_env()
    assert "SSSSSS" not in str(info.value)
    env.delenv("PA_NTFY_TOKEN")
    env.setenv("PA_NOTIFY_APPROVAL", "ntfy")
    with pytest.raises(ConfigError, match="cannot be ntfy"):
        Config.from_env()
    env.delenv("PA_NOTIFY_APPROVAL")
    env.delenv("PA_NTFY_URL")
    env.setenv("PA_NOTIFY_INFO", "ntfy")
    with pytest.raises(ConfigError, match="PA_NOTIFY_INFO=ntfy needs PA_NTFY_URL"):
        Config.from_env()


def test_ntfy_is_opt_in_and_the_default_rule_is_unchanged(env, tmp_path):
    env.setenv("PA_NTFY_URL", URL)
    config = Config.from_env()
    for kind in notify.KINDS:
        assert "ntfy" not in resolve_route(config, kind, telegram_ready=False)
        assert "ntfy" not in resolve_route(config, kind, telegram_ready=True)


def test_secrets_never_reach_the_claude_process():
    base = {"PATH": "/usr/bin", "HOME": "/home/x", "PA_NTFY_URL": URL, "PA_NTFY_TOKEN": TOKEN}
    child = build_env(BridgeConfig(workdir=Path("/w"), config_dir=Path("/c")), base=base)
    assert URL not in child.values() and TOKEN not in child.values()


class Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_client_sends_plain_text_with_bearer_and_no_markdown():
    seen = []

    def opener(request, timeout):
        seen.append(request)
        return Response(b"{}")

    assert NtfyClient(URL, TOKEN, opener=opener).post("Brief\nline two", title="Agent Brief", priority=9)
    request = seen[0]
    assert request.full_url == URL and request.get_method() == "POST" and request.data == b"Brief\nline two"
    headers = {k.lower(): v for k, v in request.header_items()}
    assert headers["authorization"] == f"Bearer {TOKEN}" and headers["markdown"] == "no"
    assert headers["priority"] == "5" and headers["title"] == "Agent Brief"


def test_client_failures_return_false_and_never_log_url_or_token(caplog):
    caplog.set_level(logging.DEBUG)

    def boom(request, timeout):
        raise OSError(f"connect to {URL} with {TOKEN} failed")

    assert NtfyClient(URL, TOKEN, opener=boom).post("x") is False
    assert "k3x9q2" not in caplog.text and TOKEN not in caplog.text
    assert "NtfyClient(<hidden>)" == repr(NtfyClient(URL, TOKEN))


class Client:
    def __init__(self):
        self.posts = []

    def post(self, text):
        self.posts.append(text)
        return True


def test_target_splits_in_utf8_bytes_and_keeps_text_inert(tmp_path):
    target = NtfyTarget(Client())
    text = "ä" * 5000  # 10000 bytes
    chunks = split_message(text, target.limit, target.max_chunks, target.measure)
    assert len(chunks) == 3 and all(target.measure(c) <= notify.NTFY_LIMIT for c in chunks)
    assert target.inert("see https://evil.example/x ‮") == "see https[:]//evil.example/x"


def test_notifier_delivers_to_ntfy_when_routed(tmp_path, env):
    env.setenv("PA_NTFY_URL", URL)
    env.setenv("PA_NOTIFY_INFO", "ntfy")
    config = Config.from_env()
    notifier = Notifier.from_config(config, run_thread=False)
    assert isinstance(notifier.targets["ntfy"], NtfyTarget) and "k3x9q2" not in repr(notifier)
    client = Client()
    notifier.targets["ntfy"] = NtfyTarget(client)
    notifier.send("info", info_notice("brief_gave_up"))
    assert len(client.posts) == 1 and client.posts[0]
