"""The Telegram Bot API client: requests, errors, backoff, pacing and token redaction."""

import http.server
import json
import logging
import socket
import threading
import traceback
from pathlib import Path

import pytest

from fake_telegram import TOKEN, FakeBotAPI
from personal_agent import telegram
from personal_agent.bridge import BridgeConfig, build_env
from personal_agent.config import Config
from personal_agent.telegram import (
    RedactingFilter,
    TelegramClient,
    TelegramError,
    install_redaction,
    redact,
    validate_api_base,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any connection to a non-loopback address fails: no test can reach the network."""
    real = socket.socket.connect

    def guarded(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and host not in ("127.0.0.1", "::1", "localhost"):
            raise OSError("network disabled in tests")
        return real(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 3))
        self.now += seconds


@pytest.fixture
def api():
    fake = FakeBotAPI()
    yield fake
    fake.close()


def client_for(api, clock=None):
    clock = clock or Clock()
    return TelegramClient(TOKEN, api.base, sleep=clock.sleep, clock=clock), clock


def test_requests_have_the_right_shape(api):
    client, clock = client_for(api)
    assert client.get_me()["username"] == "pa_test_bot"
    assert client.bot_id == 123456789 and repr(client) == "TelegramClient(<hidden>)"
    api.push_message(text="hi")
    updates = client.get_updates(None, timeout_s=1)
    assert updates and updates[0]["message"]["text"] == "hi"
    body = api.calls_of("getUpdates")[0]
    assert body["allowed_updates"] == ["message", "callback_query"] and body["limit"] == 20 and body["timeout"] == 1
    assert "offset" not in body
    client.get_updates(7, timeout_s=0)
    assert api.calls_of("getUpdates")[1]["offset"] == 7
    markup = {"inline_keyboard": [[{"text": "A", "callback_data": "x" * 22}]]}
    message_id = client.send_message(4242, "hello https://e.vil", markup, deadline=clock() + 30)
    sent = api.calls_of("sendMessage")[0]
    assert isinstance(message_id, int) and "parse_mode" not in sent
    assert sent["link_preview_options"] == {"is_disabled": True} and sent["reply_markup"] == markup
    with pytest.raises(ValueError):
        client.send_message(4242, "😀" * 2049, deadline=clock() + 30)  # 4098 UTF-16 units: split first
    assert all(path.startswith("/bot<token>/") for path in api.paths)


def test_429_cooldown_and_deadline(api):
    client, clock = client_for(api)
    api.fail("sendMessage", 429, retry_after=3)
    client.send_message(4242, "x", deadline=clock() + 30)
    assert 3 in clock.sleeps and len(api.calls_of("sendMessage")) == 2
    api.fail("sendMessage", 429, retry_after=120)
    with pytest.raises(TelegramError) as info:
        client.send_message(4242, "y", deadline=clock() + 30)
    assert info.value.kind == "rate_limited" and info.value.retry_after == 120
    # The cooldown is per method class and chat: other chats and button answers still work.
    client.send_message(9999, "other chat", deadline=clock() + 30)
    client.answer_callback_query("q1", "ok")
    with pytest.raises(TelegramError) as info:
        client.call("sendMessage", {"chat_id": 4242, "text": "z"})
    assert info.value.kind == "rate_limited"
    api.fail("answerCallbackQuery", 429, retry_after=60)
    with pytest.raises(TelegramError):
        client.answer_callback_query("q2")
    client.send_message(5555, "an approval prompt is not blocked by button spam", deadline=clock() + 30)


def test_backoff_on_server_errors_is_capped_by_the_deadline(api):
    client, clock = client_for(api)
    api.fail("sendMessage", 500, times=2)
    client.send_message(4242, "x", deadline=clock() + 30)
    assert clock.sleeps[:2] == [1, 2]
    api.fail("sendMessage", 502, times=5)
    clock.sleeps.clear()
    with pytest.raises(TelegramError) as info:
        client.send_message(4242, "x", deadline=clock() + 2.5)
    assert info.value.kind == "http" and sum(clock.sleeps) < 2.5


def test_unauthorized_is_confirmed_with_get_me(api):
    client, clock = client_for(api)
    api.fail("sendMessage", 401)  # a transient or proxied 401: getMe still works
    with pytest.raises(TelegramError) as info:
        client.send_message(4242, "x", deadline=clock() + 5)
    assert info.value.kind == "api" and not client.rejected
    api.fail("sendMessage", 401)
    api.fail("getMe", 401)
    with pytest.raises(TelegramError) as info:
        client.send_message(4242, "x", deadline=clock() + 5)
    assert info.value.kind == "unauthorized" and client.rejected
    before = len(api.calls)
    with pytest.raises(TelegramError):
        client.get_me()
    assert len(api.calls) == before  # no request at all once rejected


def test_conflict_garbage_and_too_large(api, monkeypatch):
    client, clock = client_for(api)
    api.fail("getUpdates", 409)
    with pytest.raises(TelegramError) as info:
        client.get_updates(None, timeout_s=0)
    assert info.value.kind == "conflict"
    api.fail("getMe", 200, body=b"<html>not json</html>")
    with pytest.raises(TelegramError) as info:
        client.get_me()
    assert info.value.kind == "bad_response"
    for i in range(3):
        api.push_message(text="x" * 300)
    monkeypatch.setattr(telegram, "MAX_UPDATES_BYTES", 200)
    with pytest.raises(TelegramError) as info:
        client.get_updates(None, timeout_s=0)
    assert info.value.kind == "too_large" and info.value.first_update_id == 1


def test_pacing_per_chat_and_answers_unpaced(api):
    client, clock = client_for(api)
    client.send_message(4242, "a", deadline=clock() + 30)
    client.send_message(4242, "b", deadline=clock() + 30)
    assert clock.sleeps == [telegram.SEND_GAP_S]
    clock.sleeps.clear()
    client.send_message(777, "c", deadline=clock() + 30)
    client.answer_callback_query("q", "x")
    client.answer_callback_query("q2", "x")
    assert clock.sleeps == []


def test_validate_api_base():
    assert validate_api_base("https://api.telegram.org") == "https://api.telegram.org"
    assert validate_api_base("http://127.0.0.1:8081") == "http://127.0.0.1:8081"
    assert validate_api_base("http://localhost:9") == "http://localhost:9"
    assert validate_api_base("http://[::1]:9/") == "http://[::1]:9"
    for bad in ("http://example.com", "https://evil.example/x", "http://user:pw@127.0.0.1:1",  # pragma: allowlist secret
                "http://127.0.0.1:1/path", "http://127.0.0.1:1?x=1", "ftp://127.0.0.1", "https://api.telegram.org/x"):
        with pytest.raises(ValueError):
            validate_api_base(bad)
    with pytest.raises(ValueError):
        TelegramClient("not-a-token")


def test_redirects_are_not_followed():
    hits = []

    class Target(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()

    target = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Target)
    threading.Thread(target=target.serve_forever, daemon=True).start()

    class Redirect(Target):
        def do_POST(self):
            self.send_response(307)
            self.send_header("Location", f"http://127.0.0.1:{target.server_address[1]}/stolen")
            self.send_header("Content-Length", "0")
            self.end_headers()

    redirect = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=redirect.serve_forever, daemon=True).start()
    try:
        client = TelegramClient(TOKEN, f"http://127.0.0.1:{redirect.server_address[1]}")
        with pytest.raises(TelegramError):
            client.get_me()
        assert hits == []
    finally:
        target.shutdown()
        redirect.shutdown()


def _assert_clean(exc: BaseException, caplog_text: str = ""):
    secret = TOKEN.split(":")[1]
    assert exc.__context__ is None and exc.__cause__ is None
    for text in (str(exc), repr(exc), "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
                 caplog_text):
        assert TOKEN not in text and secret not in text


def test_the_token_never_appears_in_errors_or_logs(api, caplog):
    caplog.set_level(logging.DEBUG)
    install_redaction()
    clock = Clock()
    # network error: nothing listens on the port
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
    client = TelegramClient(TOKEN, f"http://127.0.0.1:{dead_port}", sleep=clock.sleep, clock=clock)
    with pytest.raises(TelegramError) as info:
        client.get_me()
    assert info.value.kind == "network"
    _assert_clean(info.value)
    # timeout
    api.fail("getMe", 200, delay=1.0, body=b'{"ok":true,"result":{"id":1}}')
    slow = TelegramClient(TOKEN, api.base, timeout_s=0.2)
    with pytest.raises(TelegramError) as info:
        slow.get_me()
    _assert_clean(info.value)
    # 401 and garbage
    good, _ = client_for(api)
    api.fail("getMe", 401)
    with pytest.raises(TelegramError) as info:
        good.get_me()
    _assert_clean(info.value)
    api.fail("getUpdates", 200, body=b"garbage " + TOKEN.encode())
    fresh, _ = client_for(api)
    with pytest.raises(TelegramError) as info:
        fresh.get_updates(None, 0)
    _assert_clean(info.value)
    # a description that echoes the token is redacted too
    api.fail("sendMessage", 400, body=json.dumps({"ok": False, "error_code": 400,
                                                  "description": f"bad bot{TOKEN}"}).encode())
    with pytest.raises(TelegramError) as info:
        fresh.send_message(1, "x", deadline=1e12)
    assert TOKEN not in info.value.description
    _assert_clean(info.value, caplog.text)
    config = Config(state_dir=Path("/nonexistent"), status_dir=Path("/nonexistent"), telegram_token=TOKEN)
    assert TOKEN not in repr(config) and TOKEN not in repr(client)


def test_redacting_filter_scrubs_messages_args_and_tracebacks(caplog):
    record_handler = logging.Handler()
    seen = []
    record_handler.emit = lambda record: seen.append(logging.Formatter("%(message)s").format(record))
    record_handler.addFilter(RedactingFilter())
    logger = logging.getLogger("test.redaction")
    logger.addHandler(record_handler)
    logger.propagate = False
    try:
        logger.warning("calling %s", f"https://api.telegram.org/bot{TOKEN}/getMe")
        try:
            raise RuntimeError(f"failed: /bot{TOKEN}/sendMessage")
        except RuntimeError:
            logger.exception("boom")
        logger.warning("stack", stack_info=True)
    finally:
        logger.removeHandler(record_handler)
    joined = "\n".join(seen)
    assert TOKEN not in joined and "<telegram-token>" in joined and "Traceback" in joined
    assert redact(f"x {TOKEN} y") == "x <telegram-token> y"
    assert redact("123456789:short") == "123456789:short"


def test_the_token_never_reaches_the_claude_process():
    base = {"PATH": "/usr/bin", "HOME": "/home/x", "PA_TELEGRAM_TOKEN": TOKEN, "TELEGRAM_TOKEN": TOKEN}
    env = build_env(BridgeConfig(workdir=Path("/w"), config_dir=Path("/c")), base=base)
    assert TOKEN not in json.dumps(env)
    from personal_agent import bridge

    assert bridge.ENV_PASSTHROUGH == ("PATH", "HOME", "LANG", "LC_ALL", "TZ")  # an allowlist, pinned
