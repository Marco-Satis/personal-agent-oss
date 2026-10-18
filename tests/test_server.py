import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from personal_agent.config import Config, ConfigError, TwilioConfig
from personal_agent.server import Agent, make_server
from personal_agent.telephony import FakeProvider, twilio_signature

TOKEN = "t" * 32
PIN = "482913"
PUBLIC = "https://agent.example.org"


def config(tmp_path, provider="fake"):
    return Config(
        state_dir=tmp_path / "state",
        status_dir=tmp_path / "sessions",
        port=0,
        api_token=TOKEN,
        provider=provider,
        call_to="+447700900123",
        call_pin=PIN,
        public_url=PUBLIC,
        min_call_interval_min=0,
        twilio=TwilioConfig("ACx", "secret-auth", "+447700900000") if provider == "twilio" else None,
    )


@pytest.fixture
def running(tmp_path, request):
    provider_name = getattr(request, "param", "fake")
    fake = FakeProvider()
    agent = Agent(config(tmp_path, provider_name), provider=fake)
    server = make_server(agent)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield agent, fake, base
    server.shutdown()
    server.server_close()


def post(url, data: bytes, headers=None):
    req = urllib.request.Request(url, data=data, method="POST", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def escalate(base, payload, token=TOKEN):
    return post(
        f"{base}/escalate",
        json.dumps(payload).encode(),
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )


def test_escalate_requires_token(running):
    _, fake, base = running
    code, _ = escalate(base, {"key": "a", "severity": "critical"}, token="wrong")
    assert code == 401
    assert fake.placed == []


def test_escalate_places_call_and_fake_flow(running):
    agent, fake, base = running
    code, body = escalate(base, {"key": "disk", "severity": "critical", "summary": "Disk full"})
    assert code == 200 and json.loads(body)["called"] is True
    (to, call_id) = fake.placed[0]
    assert to == "+447700900123"

    q = urllib.parse.urlencode({"cid": call_id})
    code, xml = post(f"{base}/twilio/voice?{q}", b"")
    assert code == 200 and "Disk full" not in xml and "PIN" in xml
    code, xml = post(f"{base}/twilio/gather?{q}", urllib.parse.urlencode({"Digits": PIN}).encode())
    assert "Disk full" in xml


def test_unknown_call_hangs_up(running):
    _, _, base = running
    code, xml = post(f"{base}/twilio/voice?cid=nope", b"")
    assert code == 200 and "<Hangup/>" in xml


def test_body_limit(running):
    _, _, base = running
    code, _ = post(f"{base}/escalate", b"x" * 20000, {"Authorization": f"Bearer {TOKEN}"})
    assert code == 413


@pytest.mark.parametrize("running", ["twilio"], indirect=True)
def test_twilio_webhook_needs_valid_signature(running):
    agent, fake, base = running
    escalate(base, {"key": "disk", "severity": "critical", "summary": "Disk full"})
    call_id = fake.placed[0][1]
    path = f"/twilio/gather?{urllib.parse.urlencode({'cid': call_id})}"
    params = {"Digits": PIN, "CallSid": "CA1"}
    body = urllib.parse.urlencode(params).encode()

    code, _ = post(base + path, body, {"X-Twilio-Signature": "forged"})
    assert code == 403

    agent.calls.get(call_id).start()
    sig = twilio_signature("secret-auth", PUBLIC + path, params)
    code, xml = post(base + path, body, {"X-Twilio-Signature": sig})
    assert code == 200 and "Disk full" in xml


HOOK = "https://discord.com/api/webhooks/1/abcdef"
BRIEF_ENV = ("PA_BRIEF_TIME", "PA_BRIEF_GRACE_MIN", "PA_BRIEF_POLISH", "PA_BRIEF_DISCORD", "PA_DISCORD_WEBHOOK")


@pytest.fixture
def brief_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PA_STATE_DIR", str(tmp_path / "state"))
    for name in BRIEF_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_brief_config_parsing(brief_env):
    config = Config.from_env()
    assert config.brief_time is None and config.brief_grace_min == 120
    assert config.brief_polish is False and config.brief_discord == "counts"
    for raw, expected in (("7:05", (7, 5)), ("07:30", (7, 30)), ("23:59", (23, 59)), ("0:00", (0, 0))):
        brief_env.setenv("PA_BRIEF_TIME", raw)
        assert Config.from_env().brief_time == expected
    for bad in ("24:00", "7", "07:60", "7:5", "07:30:00", "ab:cd"):
        brief_env.setenv("PA_BRIEF_TIME", bad)
        with pytest.raises(ConfigError) as info:
            Config.from_env()
        assert str(info.value) == "PA_BRIEF_TIME must look like 07:30"  # never echoes the value
    brief_env.delenv("PA_BRIEF_TIME")
    for raw, ok in (("1", True), ("720", True), ("0", False), ("721", False), ("x", False)):
        brief_env.setenv("PA_BRIEF_GRACE_MIN", raw)
        if ok:
            assert Config.from_env().brief_grace_min == int(raw)
        else:
            with pytest.raises(ConfigError):
                Config.from_env()
    brief_env.delenv("PA_BRIEF_GRACE_MIN")
    for raw, expected in (("1", True), ("true", True), ("YES", True), ("0", False), ("no", False)):
        brief_env.setenv("PA_BRIEF_POLISH", raw)
        assert Config.from_env().brief_polish is expected
    brief_env.setenv("PA_BRIEF_DISCORD", "FULL")
    assert Config.from_env().brief_discord == "full"
    brief_env.setenv("PA_BRIEF_DISCORD", "everything")
    with pytest.raises(ConfigError, match="PA_BRIEF_DISCORD"):
        Config.from_env()


def test_validate_for_serve_brief_rules(tmp_path):
    base = config(tmp_path)
    base.validate_for_serve()
    Config(**{**base.__dict__, "discord_webhook": HOOK}).validate_for_serve()  # webhook alone: brief only
    with pytest.raises(ConfigError, match="PA_PUBLIC_APPROVAL_URL needs PA_DISCORD_WEBHOOK"):
        Config(**{**base.__dict__, "public_approval_url": PUBLIC}).validate_for_serve()
    with pytest.raises(ConfigError, match="PA_BRIEF_TIME needs PA_DISCORD_WEBHOOK"):
        Config(**{**base.__dict__, "brief_time": (7, 30)}).validate_for_serve()
    Config(**{**base.__dict__, "brief_time": (7, 30), "discord_webhook": HOOK}).validate_for_serve()


def test_brief_loop_survives_errors_and_stops():
    from personal_agent.server import _brief_loop

    calls = []
    stop = threading.Event()

    class Scheduler:
        def tick(self):
            calls.append(1)
            if len(calls) == 3:
                stop.set()
            raise RuntimeError("boom")

    thread = threading.Thread(target=_brief_loop, args=(Scheduler(), stop, 0.01))
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive() and len(calls) == 3


def test_brief_thread_only_with_brief_time(tmp_path, caplog, monkeypatch):
    import logging

    from personal_agent import brief
    from personal_agent.server import start_brief_thread

    ticks = []
    monkeypatch.setattr(brief.BriefScheduler, "tick", lambda self: ticks.append(1) or "not_due")  # no network
    stop = threading.Event()
    assert start_brief_thread(config(tmp_path), stop) is None
    cfg = Config(**{**config(tmp_path).__dict__, "brief_time": (6, 5), "discord_webhook": HOOK,
                    "vault_dir": tmp_path / "vault"})
    with caplog.at_level(logging.INFO, logger="personal_agent.server"):
        thread = start_brief_thread(cfg, stop)
    try:
        assert thread is not None and thread.daemon and thread.is_alive()
        assert "morning brief daily at 06:05 (grace 120 min)" in caplog.text
        assert "webhooks" not in caplog.text
        for _ in range(500):
            if ticks:
                break
            time.sleep(0.01)
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive() and ticks


def test_waiting_session_watch(tmp_path):
    import time

    cfg = config(tmp_path)
    cfg = Config(**{**cfg.__dict__, "waiting_escalate_min": 15})
    cfg.status_dir.mkdir(parents=True)
    now = time.time()
    (cfg.status_dir / "s1.json").write_text(
        json.dumps({"state": "waiting", "project": "shop", "since": now - 1200, "updated_at": now})
    )
    fake = FakeProvider()
    agent = Agent(cfg, provider=fake)
    agent.check_waiting_sessions()
    agent.check_waiting_sessions()
    assert len(fake.placed) == 1  # deduplicated
