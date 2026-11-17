"""C3a: phone shortcuts talk to the assistant over POST /chat (tailnet only, device tokens)."""

import json
import os
import stat
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from personal_agent import assistant as assistant_mod
from personal_agent.approval import MAIN_LABELS
from personal_agent.cli import main
from personal_agent.config import Config
from personal_agent.devchat import DEVICES_FILE, DeviceChat, DeviceError, DeviceStore, DenyingDeviceChannel
from personal_agent.server import Agent, make_server
from personal_agent.telephony import FakeProvider

IDENTITY = {"Tailscale-User-Login": "owner@example.org"}  # what tailscale serve adds for tailnet traffic


class FakeRuntime:
    def __init__(self, say=None):
        self.say = say
        self.turns = []
        self.router = SimpleNamespace(is_tier1=lambda text: text.startswith("fokus"))
        self.parts = SimpleNamespace(config_dir="/c")

    def turn(self, text):
        self.turns.append(text)
        if text == "needs approval" and self.say:
            self.say("This needs an approval (Write).")
        return f"answer to {text}"

    def idle_close(self):
        pass

    def close(self):
        pass


def make_config(tmp_path, **kw):
    base = dict(state_dir=tmp_path / "state", status_dir=tmp_path / "s", port=0, approval_pin="918273",
                telegram_max_turns_per_hour=2, claude_oauth_token="synthetic-oauth", chat_devices=True)
    base.update(kw)
    return Config(**base)


@pytest.fixture
def chat(tmp_path, monkeypatch):
    monkeypatch.setattr(assistant_mod, "reply_lines", lambda reply, config_dir, **kw: [str(reply)])
    config = make_config(tmp_path)
    runtime = {}

    def factory(say):
        runtime["r"] = FakeRuntime(say)
        return runtime["r"]

    devices = DeviceStore(config.state_dir)
    service = DeviceChat(config, devices, factory)
    service.runtime = runtime
    return service


# --- device tokens ------------------------------------------------------------------------------------


def test_tokens_are_shown_once_and_stored_hashed(tmp_path):
    store = DeviceStore(tmp_path)
    token = store.add("iphone")
    assert token.startswith("pad_") and len(token) > 40
    raw = (tmp_path / DEVICES_FILE).read_text()
    assert token not in raw and "iphone" in raw
    assert stat.S_IMODE(os.stat(tmp_path / DEVICES_FILE).st_mode) == 0o600
    assert store.verify(token) == "iphone"
    assert store.verify(token + "x") is None and store.verify("") is None and store.verify("pad_") is None
    assert store.list()[0][:2] == ("iphone", True)


def test_revoke_stops_the_token_and_names_are_checked(tmp_path):
    store = DeviceStore(tmp_path)
    token = store.add("iphone")
    with pytest.raises(DeviceError, match="exists"):
        store.add("iphone")
    assert store.revoke("iphone") and store.verify(token) is None
    assert not store.revoke("iphone")
    assert store.verify(store.add("iphone")) == "iphone"  # a new token after revoking
    for bad in ("", "iPhone", "a b", "-x", "x" * 33):
        with pytest.raises(DeviceError):
            store.add(bad)


# --- the chat service ------------------------------------------------------------------------------------


def run(service):
    while service.work_once(timeout=0.01):
        pass


def test_message_round_trip_and_reply_picked_up_once(chat):
    code, payload = chat.submit("iphone", "  Was steht heute an?  ")
    assert code == 202 and payload["state"] == "queued"
    run(chat)
    assert chat.runtime["r"].turns == ["Was steht heute an?"]
    assert chat.result("iphone", payload["id"], 0) == (200, {"reply": "answer to Was steht heute an?"})
    assert chat.result("iphone", payload["id"], 0)[0] == 404


def test_other_devices_cannot_read_a_reply(chat):
    _, payload = chat.submit("iphone", "privat")
    run(chat)
    assert chat.result("ipad", payload["id"], 0) == (404, {"error": "not found"})
    assert chat.result("iphone", "../../etc", 0)[0] == 404


def test_pins_are_never_handled(chat):
    assert chat.submit("iphone", "918273") == (200, {"reply": chat.t["pin"]})
    assert chat.submit("iphone", "12345678")[0] == 200
    run(chat)
    assert "r" not in chat.runtime  # nothing reached the assistant


def test_rate_limits(chat):
    codes = [chat.submit("iphone", f"fokus {n}")[0] for n in range(12)]
    assert codes.count(429) >= 2  # per minute and queue size
    run(chat)


def test_model_turns_per_hour_are_capped_but_fixed_commands_work(chat):
    for text in ("a", "b", "c"):
        chat.submit("iphone", text)
    chat.submit("iphone", "fokus")
    run(chat)
    assert chat.runtime["r"].turns == ["a", "b", "fokus"]


def test_approval_hint_is_part_of_the_reply(chat):
    _, payload = chat.submit("iphone", "needs approval")
    run(chat)
    reply = chat.result("iphone", payload["id"], 0)[1]["reply"]
    assert reply.splitlines() == ["This needs an approval (Write).", "answer to needs approval"]


def test_without_a_login_only_fixed_commands_run(tmp_path, monkeypatch):
    monkeypatch.setattr(assistant_mod, "reply_lines", lambda reply, config_dir, **kw: [str(reply)])
    config = make_config(tmp_path)
    runtimes = []
    service = DeviceChat(config, DeviceStore(config.state_dir),
                         lambda say: runtimes.append(FakeRuntime(say)) or runtimes[-1], has_login=lambda: False)
    _, question = service.submit("iphone", "was ist 17 mal 23")
    _, command = service.submit("iphone", "fokus")
    run(service)
    assert service.result("iphone", question["id"], 0)[1]["reply"] == service.t["no_login"]
    assert runtimes[0].turns == ["fokus"]


def test_denying_channel_says_where_to_approve():
    said = []
    assert DenyingDeviceChannel(said.append, "de").ask(SimpleNamespace(tool_name="Write"), 0) is False
    assert "Freigabe (Write)" in said[0]


def test_device_label_exists():
    assert "device" in MAIN_LABELS


# --- HTTP: tailnet only, token required -----------------------------------------------------------------------


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(assistant_mod, "reply_lines", lambda reply, config_dir, **kw: [str(reply)])
    config = make_config(tmp_path)
    agent = Agent(config, provider=FakeProvider())
    devices = DeviceStore(config.state_dir)
    token = devices.add("iphone")
    agent.device_chat = DeviceChat(config, devices, lambda say: FakeRuntime(say))
    agent.device_chat.start()
    httpd = make_server(agent)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", token, agent
    httpd.shutdown()
    httpd.server_close()
    agent.device_chat.close(1)


def request(url, data=None, headers=None, method="POST"):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def test_chat_over_http(server):
    base, token, _ = server
    headers = {**IDENTITY, "Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    code, payload = request(f"{base}/chat", json.dumps({"text": "hallo"}).encode(), headers)
    assert (code, payload) == (200, {"reply": "answer to hallo"})


def test_without_tailnet_identity_chat_does_not_exist(server):
    base, token, _ = server
    # Funnel (public) traffic never carries the identity header: the endpoint is not there
    code, _ = request(f"{base}/chat", b'{"text": "x"}', {"Authorization": f"Bearer {token}"})
    assert code == 404
    code, _ = request(f"{base}/chat", b'{"text": "x"}', {**IDENTITY, "Tailscale-User-Login": "   "})
    assert code == 404


def test_wrong_or_revoked_token_is_unauthorized(server):
    base, token, agent = server
    for auth in ("", "Bearer nope", f"Basic {token}", f"Bearer {token}x"):
        code, _ = request(f"{base}/chat", b'{"text": "x"}', {**IDENTITY, "Authorization": auth})
        assert code == 401
    agent.device_chat.devices.revoke("iphone")
    code, _ = request(f"{base}/chat", b'{"text": "x"}', {**IDENTITY, "Authorization": f"Bearer {token}"})
    assert code == 401


def test_bad_body_and_unknown_job(server):
    base, token, _ = server
    headers = {**IDENTITY, "Authorization": f"Bearer {token}"}
    assert request(f"{base}/chat", b"not json", headers)[0] == 400
    assert request(f"{base}/chat", b"[1]", headers)[0] == 400
    assert request(f"{base}/chat", b'{"text": ""}', headers)[0] == 400
    assert request(f"{base}/chat/{'a' * 22}?wait=0", None, headers, "GET")[0] == 404


def test_chat_is_off_by_default(tmp_path):
    config = make_config(tmp_path, chat_devices=False)
    agent = Agent(config, provider=FakeProvider())
    assert agent.device_chat is None
    httpd = make_server(agent)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        code, _ = request(f"http://127.0.0.1:{httpd.server_address[1]}/chat", b'{"text": "x"}',
                          {**IDENTITY, "Authorization": "Bearer pad_x"})
        assert code == 404
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cli_devices(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PA_STATE_DIR", str(tmp_path / "state"))
    assert main(["devices", "add", "iphone"]) == 0
    out = capsys.readouterr().out
    token = out.splitlines()[1]
    assert token.startswith("pad_") and "PA_CHAT_DEVICES is off" in out
    assert main(["devices", "list"]) == 0
    listed = capsys.readouterr().out
    assert "iphone" in listed and "active" in listed and token not in listed
    assert main(["devices", "revoke", "iphone"]) == 0
    assert main(["devices", "revoke", "iphone"]) == 1


def test_slow_reply_is_picked_up_later(server, monkeypatch):
    import time as _time

    import personal_agent.server as server_mod

    base, token, agent = server
    monkeypatch.setattr(server_mod, "WAIT_S", 0.2)
    gate = threading.Event()

    class Slow(FakeRuntime):
        def turn(self, text):
            gate.wait(5)
            return super().turn(text)

    agent.device_chat._runtime = Slow()
    headers = {**IDENTITY, "Authorization": f"Bearer {token}"}
    code, payload = request(f"{base}/chat", b'{"text": "lang"}', headers)
    assert code == 202 and payload["state"] in ("queued", "running")
    gate.set()
    deadline = _time.monotonic() + 5
    while True:
        code, result = request(f"{base}/chat/{payload['id']}?wait=1", None, headers, "GET")
        if code == 200 or _time.monotonic() > deadline:
            break
    assert (code, result) == (200, {"reply": "answer to lang"})
