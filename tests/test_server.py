import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from personal_agent.config import Config, TwilioConfig
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
