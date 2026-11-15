"""Talking to the assistant over ntfy (ntfy_inbox.py): one topic, no loops, no approvals, no PINs."""

import io
import json
import os
import stat
from types import SimpleNamespace

import pytest

from personal_agent import assistant as assistant_mod
from personal_agent.approval import ApprovalBroker
from personal_agent.config import Config, ConfigError
from personal_agent.ntfy_inbox import STATE_FILE, DenyingChannel, NtfyInbox, is_own
from personal_agent.remote import NTFY_OWN_TAG, NtfyClient, OwnMessages

NOW = 1_800_000_000.0
URL = "https://push.example.org/agent-k3x9q2"
TOKEN = "tk_" + "A1" * 15  # synthetic  # pragma: allowlist secret


class Topic:
    """An ntfy topic stand-in: the agent's posts land here with tag and id, like on the server."""

    def __init__(self):
        self.messages = []
        self.n = 0

    def add(self, text, tags=None, at=NOW):
        self.n += 1
        message = {"id": f"m{self.n:04d}", "event": "message", "time": at, "message": text}
        if tags:
            message["tags"] = tags
        self.messages.append(message)
        return message["id"]


class FakeClient:
    """Posts like NtfyClient (tag + own id), polls the Topic from `since`."""

    def __init__(self, topic, own):
        self.topic = topic
        self.own = own
        self.polled = []

    def post(self, text):
        self.own.add(self.topic.add(text, tags=[NTFY_OWN_TAG]))
        return True

    def poll(self, since):
        self.polled.append(since)
        ids = [m["id"] for m in self.topic.messages]
        start = ids.index(since) + 1 if since in ids else 0
        return [{"event": "open", "id": "x"}] + self.topic.messages[start:]


class FakeRuntime:
    def __init__(self):
        self.turns = []
        self.router = SimpleNamespace(is_tier1=lambda text: text == "help")
        self.parts = SimpleNamespace(config_dir="/c")
        self.reset = 0

    def turn(self, text):
        self.turns.append(text)
        return f"answer to {text}"

    def reset_conversation(self):
        self.reset += 1

    def idle_close(self):
        pass

    def close(self):
        pass


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(assistant_mod, "reply_lines", lambda reply, config_dir, **kw: [str(reply)])
    config = Config(state_dir=tmp_path / "state", status_dir=tmp_path / "s", approval_pin="918273",
                    telegram_max_turns_per_hour=30, claude_oauth_token="synthetic-oauth")
    topic, own, runtime = Topic(), OwnMessages(), FakeRuntime()
    client = FakeClient(topic, own)
    inbox = NtfyInbox(config, client, lambda: runtime, own=own, clock=lambda: NOW + 5)
    return SimpleNamespace(config=config, topic=topic, own=own, runtime=runtime, client=client, inbox=inbox)


def drain(inbox):
    while inbox.work_once(timeout=0.01):
        pass


def test_the_agent_never_answers_its_own_messages(setup):
    s = setup
    s.inbox.client.post("Morning brief: 3 tasks")  # a notice of the agent itself
    s.topic.add("what is on today?")               # the owner
    for _ in range(5):                             # several rounds: replies must not start a loop
        s.inbox.poll_once()
        drain(s.inbox)
    assert s.runtime.turns == ["what is on today?"]
    agent_posts = [m for m in s.topic.messages if NTFY_OWN_TAG in m.get("tags", [])]
    assert [m["message"] for m in agent_posts] == ["Morning brief: 3 tasks", "answer to what is on today?"]


def test_own_messages_are_recognised_by_tag_or_by_remembered_id(setup):
    own = OwnMessages()
    own.add("abc")
    assert is_own({"id": "zzz", "tags": [NTFY_OWN_TAG]}, own)
    assert is_own({"id": "abc", "tags": []}, own)  # tag stripped somewhere: the id still catches it
    assert not is_own({"id": "zzz", "tags": ["other"]}, own)


def test_first_start_replays_nothing_and_the_position_survives_a_restart(setup, tmp_path):
    s = setup
    assert s.inbox._since == str(int(NOW + 5))  # a unix time: only newer messages
    s.topic.add("hello")
    s.inbox.poll_once()
    path = s.config.state_dir / STATE_FILE
    assert json.loads(path.read_text())["last_id"] == "m0001"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    again = NtfyInbox(s.config, s.client, lambda: s.runtime, own=s.own, clock=lambda: NOW)
    assert again._since == "m0001"


def test_pins_never_reach_the_model(setup):
    s = setup
    for text in ("482913", "918273"):  # PIN-shaped, and the configured approval PIN
        s.topic.add(text)
    s.inbox.poll_once()
    drain(s.inbox)
    assert s.runtime.turns == []
    replies = [m["message"] for m in s.topic.messages if NTFY_OWN_TAG in m.get("tags", [])]
    assert len(replies) == 2 and all("PIN" in r for r in replies)
    assert not any("918273" in r for r in replies)


def test_stale_messages_and_new_conversation(setup):
    s = setup
    s.topic.add("old question", at=NOW - 3600)
    s.topic.add("/new")
    s.inbox.poll_once()
    drain(s.inbox)
    assert s.runtime.turns == [] and s.runtime.reset == 1


def test_hourly_turn_limit_keeps_tier1_free(setup):
    s = setup
    s.config.__dict__["telegram_max_turns_per_hour"] = 1
    for text in ("q1", "q2", "help"):
        s.topic.add(text)
    s.inbox.poll_once()
    drain(s.inbox)
    assert s.runtime.turns == ["q1", "help"]


def test_approvals_are_always_denied_with_a_hint(tmp_path):
    said = []
    channel = DenyingChannel(said.append, "de")
    assert channel.ask(SimpleNamespace(tool_name="Write"), deadline=0) is False
    assert "keine Freigaben" in said[0] and "Write" in said[0]
    broker = ApprovalBroker(tmp_path, main_label="ntfy")
    assert broker.delegation_allowed is False


def test_inbound_needs_url_and_token(monkeypatch, tmp_path):
    monkeypatch.setenv("PA_STATE_DIR", str(tmp_path / "state"))
    for name in ("PA_NTFY_URL", "PA_NTFY_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PA_NTFY_INBOUND", "on")
    with pytest.raises(ConfigError, match="PA_NTFY_INBOUND needs"):
        Config.from_env()
    monkeypatch.setenv("PA_NTFY_URL", URL)
    with pytest.raises(ConfigError, match="PA_NTFY_INBOUND needs"):
        Config.from_env()
    monkeypatch.setenv("PA_NTFY_TOKEN", TOKEN)
    assert Config.from_env().ntfy_inbound is True
    monkeypatch.delenv("PA_NTFY_INBOUND")
    assert Config.from_env().ntfy_inbound is False  # off unless asked for


class Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_client_tags_every_post_remembers_ids_and_polls_with_auth():
    seen = []

    def opener(request, timeout):
        seen.append(request)
        if request.get_method() == "POST":
            return Response(b'{"id":"Ab12","event":"message"}')
        return Response(b'{"event":"open","id":"o1"}\n{"id":"Cd34","event":"message","message":"hi"}\n')

    own = OwnMessages()
    client = NtfyClient(URL, TOKEN, opener=opener, own=own)
    assert client.post("notice")
    headers = {k.lower(): v for k, v in seen[0].header_items()}
    assert headers["tags"] == NTFY_OWN_TAG and "Ab12" in own
    messages = client.poll("Ab12")
    assert seen[1].full_url == URL + "/json?poll=1&since=Ab12"
    assert {k.lower(): v for k, v in seen[1].header_items()}["authorization"] == f"Bearer {TOKEN}"
    assert [m["id"] for m in messages] == ["o1", "Cd34"]


def test_router_treats_ntfy_like_telegram_for_mail_rules():
    from personal_agent.router import Router

    config = SimpleNamespace(small_model=None, small_max_chars=0)
    router = Router(SimpleNamespace(), None, None, config, statuses=lambda: [], channel="ntfy")
    assert router.channel == "telegram"


def test_without_a_login_questions_get_a_hint_and_no_model_starts(setup):
    s = setup
    s.config.__dict__["claude_oauth_token"] = None  # and no credentials file in the empty config dir
    s.topic.add("what is on today?")
    s.topic.add("help")
    s.inbox.poll_once()
    drain(s.inbox)
    assert s.runtime.turns == ["help"]
    replies = [m["message"] for m in s.topic.messages if NTFY_OWN_TAG in m.get("tags", [])]
    assert "Claude login" in replies[0] and replies[1] == "answer to help"


def test_polling_is_gentle_and_backs_off_on_429(setup):
    from personal_agent import ntfy_inbox
    from personal_agent.remote import NtfyRateLimited

    inbox = setup.inbox
    assert ntfy_inbox.POLL_S >= 15  # one shared rate bucket for every local client of the server
    assert inbox.next_delay(5, None) == ntfy_inbox.POLL_S
    assert inbox.next_delay(ntfy_inbox.POLL_S, NtfyRateLimited("429")) == 60
    assert inbox.next_delay(60, NtfyRateLimited("429")) == 120
    assert inbox.next_delay(500, NtfyRateLimited("429")) == ntfy_inbox.MAX_BACKOFF_S
    assert inbox.next_delay(20, OSError("down")) == 40


def test_client_turns_a_429_into_rate_limited():
    import urllib.error

    from personal_agent.remote import NtfyRateLimited

    def opener(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", {}, io.BytesIO(b""))

    with pytest.raises(NtfyRateLimited):
        NtfyClient(URL, TOKEN, opener=opener).poll("1")
