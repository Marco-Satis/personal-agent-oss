import io
import json
import logging
import re
import shlex
import stat
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from personal_agent import remote
from personal_agent.approval import ApprovalRequest, Level
from personal_agent.config import Config, ConfigError
from personal_agent.remote import (
    EXPIRY_MARGIN_S,
    MAX_PENDING,
    NOTICE_LIMIT,
    ApprovalInbox,
    DiscordWebhook,
    RemoteChannel,
    render_page,
)
from personal_agent.server import PAGE_HEADERS, Agent, Handler, make_server
from personal_agent.telephony import FakeProvider

TOKEN = "t" * 32
APIN = "731946"
PUBLIC = "https://agent.tailnet.example"
HOOK = "https://discord.com/api/webhooks/123456/very-secret-hook-token"
FAKE = Path(__file__).resolve().parent / "fake_claude.py"
SUMMARY = 'file_path="notes/a.md", content=11 chars: "hello world"'


# --- helpers -------------------------------------------------------------------


class FakeNotifier:
    def __init__(self, ok=True):
        self.ok = ok
        self.posts = []
        self.posted = threading.Event()

    def post(self, text):
        self.posts.append(text)
        self.posted.set()
        return self.ok


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeResponse:
    def __init__(self, status=204):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def getcode(self):
        return self.status

    def read(self, _n=-1):
        return b""


def link_id(text):
    match = re.search(r"/approve/([A-Za-z0-9_-]{32})>", text)
    assert match, "no approval link in the notice"
    return match.group(1)


def csrf_of(page):
    return re.search(r'name="csrf" value="([^"]+)"', page).group(1)


NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http(method, url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with NO_PROXY.open(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read().decode()
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return e.code, dict(e.headers), body


def bearer(extra=None):
    return {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json", **(extra or {})}


def form(**fields):
    return urllib.parse.urlencode(fields).encode()


FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def server_config(tmp_path, **kw):
    return Config(
        state_dir=tmp_path / "state",
        status_dir=tmp_path / "sessions",
        port=0,
        api_token=TOKEN,
        call_to="+447700900123",
        call_pin="482913",
        public_approval_url=PUBLIC,
        **kw,
    )


def start_server(agent):
    server = make_server(agent)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture
def served(tmp_path):
    notifier = FakeNotifier()
    clock = Clock(time.time())
    inbox = ApprovalInbox(notifier, PUBLIC, pin=APIN, clock=clock)
    agent = Agent(server_config(tmp_path), provider=FakeProvider(), approvals=inbox)
    server, base = start_server(agent)
    yield SimpleNamespace(agent=agent, inbox=inbox, notifier=notifier, base=base, clock=clock)
    server.shutdown()
    server.server_close()


def register(served, level=2, summary=SUMMARY, timeout_s=300, tool="Write"):
    code, _, body = http(
        "POST",
        f"{served.base}/internal/approvals",
        json.dumps({"tool_name": tool, "level": level, "summary": summary, "timeout_s": timeout_s}).encode(),
        bearer(),
    )
    assert code == 200, body
    return json.loads(body)["id"]


def state_of(served, approval_id, wait=0):
    code, _, body = http("GET", f"{served.base}/internal/approvals/{approval_id}?wait={wait}", headers=bearer())
    return code, (json.loads(body).get("state") if code == 200 else None)


# --- Discord webhook -----------------------------------------------------------


def test_webhook_payload_has_no_mentions_embeds_or_input():
    captured = []

    def opener(request, timeout):
        captured.append(request)
        return FakeResponse(204)

    inbox = ApprovalInbox(DiscordWebhook(HOOK, opener=opener), PUBLIC)
    entry = inbox.register("mcp__mail__draft_reply", 2, "SECRET-INPUT body text", 300)
    assert entry is not None
    (request,) = captured
    assert request.full_url == HOOK and request.get_method() == "POST"
    payload = json.loads(request.data)
    assert payload["flags"] == 4 and payload["allowed_mentions"] == {"parse": []}
    content = payload["content"]
    assert r"mcp\_\_mail\_\_draft\_reply" in content and "level 2" in content
    assert f"<{PUBLIC}/approve/{entry.id}>" in content
    assert "SECRET-INPUT" not in content and "body text" not in content
    assert request.get_header("User-agent").startswith("personal-agent/")


def test_notice_strips_mentions_and_markup():
    notifier = FakeNotifier()
    ApprovalInbox(notifier, PUBLIC).register("mcp__x__a*b`c @everyone", 2, SUMMARY, 300)
    notice = notifier.posts[0]
    assert "@" not in notice and "`" not in notice and "*" not in notice
    assert r"mcp\_\_x\_\_a" in notice


def test_webhook_notice_in_german():
    notifier = FakeNotifier()
    ApprovalInbox(notifier, PUBLIC, language="de").register("Write", 2, SUMMARY, 300)
    assert notifier.posts[0].startswith("Freigabe nötig (Stufe 2): Write.")


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.HTTPError(HOOK, 403, "Forbidden", {}, io.BytesIO(b"")),
        ValueError(f"bad url {HOOK}"),
        urllib.error.URLError(f"cannot reach {HOOK}"),
        RuntimeError(HOOK),
    ],
)
def test_webhook_failure_never_logs_the_url(caplog, error):
    def opener(request, timeout):
        raise error

    caplog.set_level(logging.DEBUG)
    hook = DiscordWebhook(HOOK, opener=opener)
    assert hook.post("hello") is False
    assert "very-secret-hook-token" not in caplog.text and "/api/webhooks/" not in caplog.text
    assert "webhook post failed" in caplog.text
    assert "secret" not in repr(hook) and "discord" not in repr(hook)


def test_webhook_non_2xx_is_a_failure():
    assert DiscordWebhook(HOOK, opener=lambda r, timeout: FakeResponse(500)).post("x") is False


def test_webhook_does_not_follow_redirects(monkeypatch):
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    landed = []

    class Redirector(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            if self.path.startswith("/landed"):
                landed.append(self.path)
                self.send_response(204)
                self.end_headers()
                return
            self.send_response(307)
            self.send_header("Location", "/landed")
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Redirector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/api/webhooks/1/x"
        assert DiscordWebhook(url).post("hello") is False
        assert landed == []
    finally:
        server.shutdown()
        server.server_close()


# --- inbox ---------------------------------------------------------------------


def make_inbox(ok=True, pin=APIN, **kw):
    notifier = FakeNotifier(ok)
    clock = Clock()
    return ApprovalInbox(notifier, PUBLIC, pin=pin, clock=clock, **kw), notifier, clock


def test_register_creates_one_time_id_and_posts_once():
    inbox, notifier, clock = make_inbox()
    entry = inbox.register("Write", 2, SUMMARY, 120)
    assert len(entry.id) == 32 and len(notifier.posts) == 1
    assert link_id(notifier.posts[0]) == entry.id
    assert entry.expires_at == clock.now + 120 - EXPIRY_MARGIN_S
    assert entry.csrf and entry.csrf not in notifier.posts[0]


def test_register_caps_timeout_and_refuses_bad_requests():
    inbox, notifier, clock = make_inbox(max_timeout_s=60)
    assert inbox.register("Write", 2, SUMMARY, 3600).expires_at == clock.now + 60 - EXPIRY_MARGIN_S
    assert inbox.register("Read", 1, SUMMARY, 60) is None
    assert inbox.register("Write", 2, SUMMARY, EXPIRY_MARGIN_S) is None


def test_failed_notice_means_deny():
    inbox, notifier, _ = make_inbox(ok=False)
    assert inbox.register("Write", 2, SUMMARY, 120) is None
    assert inbox._entries == {}


def test_level3_without_pin_is_refused():
    inbox, notifier, _ = make_inbox(pin=None)
    assert inbox.register("mcp__mail__send", 3, SUMMARY, 120) is None
    assert notifier.posts == []


def test_max_pending():
    inbox, _, _ = make_inbox()
    for _ in range(MAX_PENDING):
        assert inbox.register("Write", 2, SUMMARY, 120) is not None
    assert inbox.register("Write", 2, SUMMARY, 120) is None


def test_notice_rate_limit():
    inbox, notifier, clock = make_inbox()
    for _ in range(NOTICE_LIMIT):
        entry = inbox.register("Write", 2, SUMMARY, 120)
        assert inbox.resolve(entry.id, entry.csrf, "deny") == "ok"
    assert inbox.register("Write", 2, SUMMARY, 120) is None
    assert len(notifier.posts) == NOTICE_LIMIT
    clock.now += 11 * 60
    assert inbox.register("Write", 2, SUMMARY, 120) is not None


def test_resolve_csrf_single_use_and_level3_confirmation():
    inbox, _, _ = make_inbox()
    entry = inbox.register("Write", 2, SUMMARY, 120)
    assert inbox.resolve(entry.id, "wrong", "allow") == "bad_csrf"
    assert inbox.resolve(entry.id, "", "allow") == "bad_csrf"
    assert inbox.resolve(entry.id, entry.csrf, "allow") == "ok"
    assert entry.state == "allowed" and entry.done.is_set()
    assert inbox.resolve(entry.id, entry.csrf, "allow") == "invalid"
    assert inbox.get(entry.id) is None

    danger = inbox.register("mcp__mail__send", 3, SUMMARY, 120)
    assert inbox.resolve(danger.id, danger.csrf, "allow") == "need_confirm"
    assert inbox.resolve(danger.id, danger.csrf, "allow", confirm="yes", pin=APIN) == "need_confirm"
    assert danger.state == "pending"
    assert inbox.resolve(danger.id, danger.csrf, "allow", confirm=" Confirm ", pin=APIN) == "ok"
    assert danger.state == "allowed"

    other = inbox.register("mcp__mail__send", 3, SUMMARY, 120)
    assert inbox.resolve(other.id, other.csrf, "deny") == "ok" and other.state == "denied"
    assert inbox.resolve("x" * 32, "c", "allow") == "invalid"


def test_three_wrong_pins_deny():
    inbox, _, _ = make_inbox()
    entry = inbox.register("mcp__mail__send", 3, SUMMARY, 120)
    assert inbox.resolve(entry.id, entry.csrf, "allow", "confirm", "000000") == "bad_pin"
    assert inbox.resolve(entry.id, entry.csrf, "allow", "confirm", "") == "bad_pin"
    assert entry.state == "pending"
    assert inbox.resolve(entry.id, entry.csrf, "allow", "confirm", "111111") == "locked"
    assert entry.state == "denied"
    assert inbox.resolve(entry.id, entry.csrf, "allow", "confirm", APIN) == "invalid"


def test_expiry_denies_and_late_click_is_invalid():
    inbox, _, clock = make_inbox()
    entry = inbox.register("Write", 2, SUMMARY, 60)
    clock.now = entry.expires_at
    assert inbox.get(entry.id) is None
    assert inbox.wait(entry.id, 0) == "expired"
    assert inbox.resolve(entry.id, entry.csrf, "allow") == "invalid"


def test_cancel_and_wait():
    inbox, _, clock = make_inbox()
    entry = inbox.register("Write", 2, SUMMARY, 60)
    assert inbox.wait(entry.id, 0) == "pending"
    assert inbox.cancel(entry.id) is True
    assert inbox.wait(entry.id, 5) == "expired"
    assert inbox.resolve(entry.id, entry.csrf, "allow") == "invalid"
    assert inbox.cancel("y" * 32) is False and inbox.wait("y" * 32, 0) is None
    clock.now += 60 + 11 * 60
    inbox.register("Write", 2, SUMMARY, 60)  # prunes
    assert entry.id not in inbox._entries


def test_wait_wakes_up_on_decision():
    inbox, _, _ = make_inbox()
    entry = inbox.register("Write", 2, SUMMARY, 60)
    threading.Timer(0.2, inbox.resolve, (entry.id, entry.csrf, "allow")).start()
    started = time.monotonic()
    assert inbox.wait(entry.id, 10) == "allowed"
    assert time.monotonic() - started < 3


def test_render_page_escapes_everything():
    inbox, _, _ = make_inbox()
    entry = inbox.register("mcp__x__send<b>", 3, '<script>alert(1)</script>"&', 60)
    page = render_page(entry)
    assert "<script>" not in page and "&lt;script&gt;" in page and "<b>" not in page
    assert 'name="confirm"' in page and 'name="pin"' in page and 'type="password"' in page
    assert "<script" not in page.lower() and 'method="post"' in page


# --- server HTTP ---------------------------------------------------------------


def test_internal_api_requires_bearer_loopback_and_config(served, tmp_path):
    body = json.dumps({"tool_name": "Write", "level": 2, "summary": SUMMARY, "timeout_s": 60}).encode()
    assert http("POST", f"{served.base}/internal/approvals", body, {"Content-Type": "application/json"})[0] == 401
    assert http("POST", f"{served.base}/internal/approvals", body, bearer({"Authorization": "Bearer x"}))[0] == 401
    for header in ("X-Forwarded-For", "Forwarded", "X-Real-IP", "Tailscale-User-Login", "X-Forwarded-Host"):
        code, _, _ = http("POST", f"{served.base}/internal/approvals", body, bearer({header: "1.2.3.4"}))
        assert code == 404, header
    assert http("POST", f"{served.base}/internal/approvals", body, bearer())[0] == 200
    assert served.notifier.posts

    agent = Agent(server_config(tmp_path), provider=FakeProvider())
    assert agent.approvals is None  # no webhook configured
    server, base = start_server(agent)
    try:
        assert http("POST", f"{base}/internal/approvals", body, bearer())[0] == 503
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    "payload",
    [
        {"tool_name": "Read", "level": 1, "summary": "", "timeout_s": 60},
        {"tool_name": "Write", "level": True, "summary": "", "timeout_s": 60},
        {"tool_name": "Write", "level": 2, "summary": "", "timeout_s": "60"},
        {"tool_name": "Write", "level": 2, "summary": "", "timeout_s": 4000},
        {"tool_name": "", "level": 2, "summary": "", "timeout_s": 60},
        {"tool_name": "Write", "level": 2, "summary": "x" * 3000, "timeout_s": 60},
        [],
    ],
)
def test_internal_api_validates_input(served, payload):
    code, _, _ = http("POST", f"{served.base}/internal/approvals", json.dumps(payload).encode(), bearer())
    assert code == 400
    assert served.notifier.posts == []


def test_escalate_is_local_only(served):
    code, _, _ = http(
        "POST", f"{served.base}/escalate", b"{}", bearer({"X-Forwarded-For": "203.0.113.9"})
    )
    assert code == 404


def test_approve_page_and_security_headers(served):
    approval_id = register(served, summary='<script>alert("x")</script>')
    code, headers, page = http("GET", f"{served.base}/approve/{approval_id}")
    assert code == 200
    for name, value in PAGE_HEADERS.items():
        assert headers[name] == value
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert "<script>" not in page and "&lt;script&gt;" in page
    assert "Write" in page and 'type="hidden" name="csrf"' in page
    assert 'name="confirm"' not in page  # level 2 needs no confirmation


@pytest.mark.parametrize("approval_id", ["x" * 32, "short", "../../etc", "a" * 33])
def test_unknown_or_malformed_id_is_404(served, approval_id):
    code, headers, page = http("GET", f"{served.base}/approve/{approval_id}")
    assert code == 404 and headers["X-Frame-Options"] == "DENY"
    code, _, _ = http("POST", f"{served.base}/approve/{approval_id}", form(csrf="c", decision="allow"), FORM)
    assert code == 404


def test_approve_post_checks_and_single_use(served, caplog):
    caplog.set_level(logging.DEBUG)
    approval_id = register(served)
    _, _, page = http("GET", f"{served.base}/approve/{approval_id}")
    csrf = csrf_of(page)
    url = f"{served.base}/approve/{approval_id}"

    assert http("POST", url, form(decision="allow"), FORM)[0] == 403  # no csrf
    assert http("POST", url, form(csrf="forged", decision="allow"), FORM)[0] == 403
    assert http("POST", url, form(csrf=csrf, decision="allow"), {"Content-Type": "text/plain"})[0] == 415
    assert http("POST", url, form(csrf=csrf, decision="allow"), {**FORM, "Origin": "https://evil.example"})[0] == 403
    assert http("POST", url, form(csrf=csrf, decision="allow"), {**FORM, "Origin": "null"})[0] == 403
    assert http("POST", url, form(csrf=csrf, decision="allow"), {**FORM, "Sec-Fetch-Site": "cross-site"})[0] == 403
    assert http("POST", url, form(csrf=csrf, decision="maybe"), FORM)[0] == 400
    assert state_of(served, approval_id) == (200, "pending")

    code, headers, page = http(
        "POST", url, form(csrf=csrf, decision="allow"),
        {**FORM, "Origin": PUBLIC, "Sec-Fetch-Site": "same-origin"},
    )
    assert code == 200 and "Approved" in page and headers["Cache-Control"] == "no-store"
    assert state_of(served, approval_id, wait=1) == (200, "allowed")

    assert http("POST", url, form(csrf=csrf, decision="allow"), FORM)[0] == 404  # replay
    assert http("GET", url)[0] == 404
    assert approval_id not in caplog.text and csrf not in caplog.text and "hello world" not in caplog.text
    assert approval_id[:6] in caplog.text


def test_too_many_form_fields_is_rejected(served):
    approval_id = register(served)
    body = "&".join(f"f{i}=1" for i in range(20)).encode()
    assert http("POST", f"{served.base}/approve/{approval_id}", body, FORM)[0] == 400


def test_level3_page_needs_confirm_and_pin(served):
    approval_id = register(served, level=3, tool="mcp__mail__send_message")
    url = f"{served.base}/approve/{approval_id}"
    _, _, page = http("GET", url)
    assert 'name="confirm"' in page and 'name="pin"' in page
    csrf = csrf_of(page)

    code, _, page = http("POST", url, form(csrf=csrf, decision="allow", pin=APIN), FORM)
    assert code == 200 and "confirm" in page and 'name="csrf"' in page
    assert state_of(served, approval_id) == (200, "pending")

    code, _, page = http("POST", url, form(csrf=csrf, decision="allow", confirm="confirm", pin="1"), FORM)
    assert code == 200 and "Wrong PIN. 2" in page

    code, _, _ = http("POST", url, form(csrf=csrf, decision="allow", confirm="confirm", pin=APIN), FORM)
    assert code == 200
    assert state_of(served, approval_id) == (200, "allowed")


def test_level3_wrong_pin_three_times_denies(served):
    approval_id = register(served, level=3, tool="mcp__mail__send_message")
    url = f"{served.base}/approve/{approval_id}"
    csrf = csrf_of(http("GET", url)[2])
    for _ in range(2):
        assert http("POST", url, form(csrf=csrf, decision="allow", confirm="confirm", pin="999999"), FORM)[0] == 200
    code, _, page = http("POST", url, form(csrf=csrf, decision="allow", confirm="confirm", pin="999999"), FORM)
    assert code == 403 and "Too many wrong PINs" in page
    assert state_of(served, approval_id) == (200, "denied")


def test_level3_deny_needs_no_confirmation(served):
    approval_id = register(served, level=3, tool="mcp__mail__send_message")
    url = f"{served.base}/approve/{approval_id}"
    csrf = csrf_of(http("GET", url)[2])
    assert http("POST", url, form(csrf=csrf, decision="deny"), FORM)[0] == 200
    assert state_of(served, approval_id) == (200, "denied")


def test_expired_link_is_404(served):
    approval_id = register(served, timeout_s=60)
    url = f"{served.base}/approve/{approval_id}"
    csrf = csrf_of(http("GET", url)[2])
    served.clock.now += 60
    assert http("GET", url)[0] == 404
    assert http("POST", url, form(csrf=csrf, decision="allow"), FORM)[0] == 404
    assert state_of(served, approval_id) == (200, "expired")


def test_bad_wait_query_is_tolerated(served):
    approval_id = register(served)
    query = "&".join(f"w{i}=1" for i in range(10))
    code, _, body = http("GET", f"{served.base}/internal/approvals/{approval_id}?{query}", headers=bearer())
    assert code == 200 and json.loads(body)["state"] == "pending"
    assert state_of(served, approval_id, wait="abc") == (200, "pending")


def test_cancel_endpoint(served):
    approval_id = register(served)
    assert http("POST", f"{served.base}/internal/approvals/{approval_id}/cancel", b"{}", {"Content-Type": "application/json"})[0] == 401
    assert http("POST", f"{served.base}/internal/approvals/{approval_id}/cancel", b"{}", bearer())[0] == 200
    assert http("GET", f"{served.base}/approve/{approval_id}")[0] == 404
    assert state_of(served, approval_id) == (200, "expired")
    assert http("POST", f"{served.base}/internal/approvals/{'z' * 32}/cancel", b"{}", bearer())[0] == 404
    assert state_of(served, "z" * 32)[0] == 404


def test_handler_has_a_socket_timeout():
    assert Handler.timeout == 15


def test_agent_builds_inbox_from_config(tmp_path):
    cfg = server_config(tmp_path, discord_webhook=HOOK, approval_pin=APIN)
    agent = Agent(cfg, provider=FakeProvider())
    assert isinstance(agent.approvals, ApprovalInbox)
    assert agent.approvals.public_base == PUBLIC
    assert HOOK not in repr(agent.approvals.notifier)


# --- remote channel ------------------------------------------------------------


def make_request(level=Level.WRITE, full=None, tool="Write"):
    return ApprovalRequest(tool_name=tool, level=level, summary=SUMMARY, tool_use_id="t1", full=full)


def run_ask(channel, request, seconds):
    result = {}
    thread = threading.Thread(
        target=lambda: result.setdefault("value", channel.ask(request, time.monotonic() + seconds))
    )
    thread.start()
    return thread, result


def click(served, decision="allow", **extra):
    assert served.notifier.posted.wait(10)
    approval_id = link_id(served.notifier.posts[-1])
    url = f"{served.base}/approve/{approval_id}"
    csrf = csrf_of(http("GET", url)[2])
    return http("POST", url, form(csrf=csrf, decision=decision, **extra), FORM)[0]


def test_remote_channel_approved_from_the_page(served, monkeypatch):
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")  # a dead proxy must not be used
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    out = []
    channel = RemoteChannel(served.base, TOKEN, threading.Lock(), write=out.append, poll_s=1)
    thread, result = run_ask(channel, make_request(), 30)
    assert click(served) == 200
    thread.join(15)
    assert result["value"] is True
    text = "\n".join(out)
    assert "approval pending remotely: Write (level 2)" in text and "[approved remotely]" in text
    assert TOKEN not in text and link_id(served.notifier.posts[-1]) not in text


def test_remote_channel_denied_from_the_page(served):
    out = []
    channel = RemoteChannel(served.base, TOKEN, threading.Lock(), write=out.append, poll_s=1)
    thread, result = run_ask(channel, make_request(), 30)
    assert click(served, "deny") == 200
    thread.join(15)
    assert result["value"] is False and "[denied remotely]" in out


def test_remote_channel_level3_with_pin(served):
    channel = RemoteChannel(served.base, TOKEN, threading.Lock(), write=lambda _l: None, poll_s=1)
    thread, result = run_ask(channel, make_request(Level.DANGER, tool="mcp__mail__send_message"), 30)
    assert click(served, confirm="confirm", pin=APIN) == 200
    thread.join(15)
    assert result["value"] is True


def test_remote_channel_times_out_and_cancels(served):
    out = []
    channel = RemoteChannel(served.base, TOKEN, threading.Lock(), write=out.append, poll_s=1)
    started = time.monotonic()
    assert channel.ask(make_request(), time.monotonic() + EXPIRY_MARGIN_S + 2) is False
    assert time.monotonic() - started < 10
    approval_id = link_id(served.notifier.posts[-1])
    assert http("GET", f"{served.base}/approve/{approval_id}")[0] == 404  # cancelled
    assert "[remote approval timed out: denied]" in out


def test_remote_channel_stops_when_told(served):
    stop = threading.Event()
    channel = RemoteChannel(served.base, TOKEN, threading.Lock(), write=lambda _l: None, poll_s=1, stop=stop.is_set)
    thread, result = run_ask(channel, make_request(), 60)
    assert served.notifier.posted.wait(10)
    stop.set()
    thread.join(10)
    assert result["value"] is False
    assert http("GET", f"{served.base}/approve/{link_id(served.notifier.posts[-1])}")[0] == 404


@pytest.mark.parametrize("failure", ["timeout", "http500", "bad_json"])
def test_remote_channel_cancels_after_a_poll_error(served, failure):
    real = remote.local_opener()

    def opener(request, timeout):
        if request.get_method() == "GET":
            if failure == "timeout":
                raise TimeoutError("poll timed out")
            if failure == "http500":
                raise urllib.error.HTTPError(request.full_url, 500, "boom", {}, io.BytesIO(b""))
            response = FakeResponse(200)
            response.read = lambda _n=-1: b"{not json"
            return response
        return real(request, timeout=timeout)

    out = []
    channel = RemoteChannel(served.base, TOKEN, threading.Lock(), write=out.append, opener=opener, poll_s=1)
    assert channel.ask(make_request(), time.monotonic() + 30) is False
    approval_id = link_id(served.notifier.posts[-1])
    assert http("GET", f"{served.base}/approve/{approval_id}")[0] == 404  # a late click shows nothing
    assert served.inbox._entries[approval_id].state == "expired"
    assert TOKEN not in "\n".join(out) and approval_id not in "\n".join(out)


def test_remote_channel_without_server_denies():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    out = []
    channel = RemoteChannel(f"http://127.0.0.1:{port}", TOKEN, threading.Lock(), write=out.append)
    assert channel.ask(make_request(), time.monotonic() + 30) is False
    assert any("remote approval failed" in line for line in out)
    assert not any(TOKEN in line for line in out)


def test_remote_channel_not_configured_denies(tmp_path):
    agent = Agent(server_config(tmp_path), provider=FakeProvider())
    server, base = start_server(agent)
    try:
        out = []
        channel = RemoteChannel(base, TOKEN, threading.Lock(), write=out.append)
        assert channel.ask(make_request(), time.monotonic() + 30) is False
        assert "[remote approvals are not configured on the server: denied]" in out
    finally:
        server.shutdown()
        server.server_close()


def test_remote_channel_wrong_token_denies(served):
    out = []
    channel = RemoteChannel(served.base, "wrong-token", threading.Lock(), write=out.append)
    assert channel.ask(make_request(), time.monotonic() + 30) is False
    assert served.notifier.posts == []
    assert any("HTTP 401" in line for line in out)


def test_remote_channel_refuses_inputs_too_large_to_review(served):
    out = []
    channel = RemoteChannel(served.base, TOKEN, threading.Lock(), write=out.append)
    assert channel.ask(make_request(Level.DANGER, full='{"to": ["a", "b"]}'), time.monotonic() + 30) is False
    assert served.notifier.posts == [] and "too large to review remotely" in out[0]


def test_remote_channel_level3_without_pin_configured_denies(tmp_path):
    notifier = FakeNotifier()
    agent = Agent(server_config(tmp_path), provider=FakeProvider(), approvals=ApprovalInbox(notifier, PUBLIC))
    server, base = start_server(agent)
    try:
        channel = RemoteChannel(base, TOKEN, threading.Lock(), write=lambda _l: None)
        assert channel.ask(make_request(Level.DANGER), time.monotonic() + 30) is False
        assert notifier.posts == []
    finally:
        server.shutdown()
        server.server_close()


# --- configuration -------------------------------------------------------------


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("PA_STATE_DIR", str(tmp_path))
    for name in ("PA_DISCORD_WEBHOOK", "PA_PUBLIC_APPROVAL_URL", "PA_APPROVAL_PIN"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.mark.parametrize(
    "value",
    [
        "http://discord.com/api/webhooks/1/secret-x",
        "https://evil.example/api/webhooks/1/secret-x",
        "https://discord.com/other/1/secret-x",
        "discord.com/api/webhooks/1/secret-x",
    ],
)
def test_webhook_config_is_validated_without_echoing_it(env, value):
    env.setenv("PA_DISCORD_WEBHOOK", value)
    with pytest.raises(ConfigError) as info:
        Config.from_env()
    assert "secret-x" not in str(info.value)


def test_webhook_config_hidden_in_repr(env):
    env.setenv("PA_DISCORD_WEBHOOK", HOOK)
    env.setenv("PA_APPROVAL_PIN", APIN)
    config = Config.from_env()
    assert config.discord_webhook == HOOK
    assert "very-secret" not in repr(config) and APIN not in repr(config)


def test_public_approval_url(env):
    env.setenv("PA_PUBLIC_APPROVAL_URL", "agent.tailnet.ts.net/")
    assert Config.from_env().public_approval_url == "https://agent.tailnet.ts.net"
    env.setenv("PA_PUBLIC_APPROVAL_URL", "https://agent.tailnet.ts.net/pa/")
    assert Config.from_env().public_approval_url == "https://agent.tailnet.ts.net/pa"
    for bad in ("http://agent.tailnet.ts.net", "https://", "https://host/?q=1"):
        env.setenv("PA_PUBLIC_APPROVAL_URL", bad)
        with pytest.raises(ConfigError):
            Config.from_env()


def test_approval_pin_config(env):
    env.setenv("PA_APPROVAL_PIN", "12ab56")
    with pytest.raises(ConfigError):
        Config.from_env()
    env.setenv("PA_APPROVAL_PIN", "12345")
    with pytest.raises(ConfigError):
        Config.from_env()


def test_trusted_clients_config(env):
    env.delenv("PA_TRUSTED_CLIENTS", raising=False)
    assert Config.from_env().trusted_clients == ()
    env.setenv("PA_TRUSTED_CLIENTS", "172.17.0.1, fd00::/8")
    assert [str(n) for n in Config.from_env().trusted_clients] == ["172.17.0.1/32", "fd00::/8"]
    for bad in ("0.0.0.0/0", "8.8.8.8", "100.64.0.0/10", "abc", "::/0"):
        env.setenv("PA_TRUSTED_CLIENTS", bad)
        with pytest.raises(ConfigError):
            Config.from_env()


def local_check(address, trusted=(), headers=None):
    handler = Handler.__new__(Handler)
    handler.client_address = (address, 40000)
    handler.headers = headers or {}
    handler.agent = SimpleNamespace(config=SimpleNamespace(trusted_clients=trusted))
    return handler._local()


def test_local_check_accepts_trusted_container_gateway():
    import ipaddress

    gateway = (ipaddress.ip_network("172.17.0.1/32"),)
    assert local_check("127.0.0.1") and local_check("::1") and local_check("::ffff:127.0.0.1")
    assert not local_check("172.17.0.1")  # a container sees host clients from the bridge gateway
    assert local_check("172.17.0.1", gateway)
    assert local_check("::ffff:172.17.0.1", gateway)
    assert not local_check("172.17.0.2", gateway)
    assert not local_check("172.17.0.1", gateway, {"X-Forwarded-For": "203.0.113.9"})
    assert not local_check("127.0.0.1", headers={"Tailscale-User-Login": "x"})


def test_validate_for_serve_remote_settings(tmp_path):
    base = server_config(tmp_path)  # public URL only
    with pytest.raises(ConfigError):
        base.validate_for_serve()
    only_hook = Config(**{**base.__dict__, "public_approval_url": None, "discord_webhook": HOOK})
    with pytest.raises(ConfigError):
        only_hook.validate_for_serve()
    no_pin = Config(**{**base.__dict__, "discord_webhook": HOOK})
    with pytest.raises(ConfigError):
        no_pin.validate_for_serve()
    Config(**{**base.__dict__, "discord_webhook": HOOK, "approval_pin": APIN}).validate_for_serve()
    Config(**{**base.__dict__, "public_approval_url": None}).validate_for_serve()


# --- chat with remote approvals, end to end with the fake CLI -------------------


@pytest.fixture
def remote_chat(tmp_path, monkeypatch, served):
    log = tmp_path / "fake.jsonl"
    wrapper = tmp_path / "claude"
    wrapper.write_text(
        f"#!/bin/sh\nFAKE_CLAUDE_LOG={shlex.quote(str(log))} exec "
        f"{shlex.quote(sys.executable)} {shlex.quote(str(FAKE))} \"$@\"\n"
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PA_STATE_DIR", str(tmp_path / "chat-state"))
    monkeypatch.setenv("PA_CLAUDE_BIN", str(wrapper))
    monkeypatch.setenv("PA_API_TOKEN", TOKEN)
    monkeypatch.setenv("PA_PORT", served.base.rsplit(":", 1)[1])
    for name in ("PA_WORKDIR", "PA_CLAUDE_CONFIG_DIR", "PA_BIND_HOST"):
        monkeypatch.delenv(name, raising=False)

    def run(lines):
        from personal_agent.cli import main

        inputs = iter(lines)
        monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
        return main(["chat", "--remote-approvals"])

    return SimpleNamespace(run=run, state=tmp_path / "chat-state")


def test_chat_with_remote_approval(remote_chat, served, capsys):
    clicker = threading.Thread(target=click, args=(served,))
    clicker.start()
    assert remote_chat.run(["WRITE a.txt", "/exit"]) == 0
    clicker.join(10)
    out = capsys.readouterr().out
    assert (remote_chat.state / "workspace" / "a.txt").read_text() == "x"
    assert "[approval pending remotely: Write (level 2), link sent]" in out
    assert "agent[3]> wrote a.txt" in out
    assert TOKEN not in out


def test_chat_remote_approvals_needs_api_token(monkeypatch, tmp_path, capsys):
    from personal_agent.cli import main

    monkeypatch.setenv("PA_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("PA_API_TOKEN", raising=False)
    assert main(["chat", "--remote-approvals"]) == 2
    assert "PA_API_TOKEN" in capsys.readouterr().err


def test_escalate_ignores_http_proxy(served, monkeypatch, capsys):
    from personal_agent.cli import main

    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("PA_STATE_DIR", str(served.agent.config.state_dir))
    monkeypatch.setenv("PA_API_TOKEN", TOKEN)
    monkeypatch.setenv("PA_PORT", served.base.rsplit(":", 1)[1])
    monkeypatch.setenv("PA_BIND_HOST", "0.0.0.0")  # mapped to loopback for the local call
    assert main(["escalate", "k", "info only", "--severity", "info"]) == 0
    assert '"called": false' in capsys.readouterr().out


def test_local_opener_has_no_proxy():
    opener = remote.local_opener()
    assert opener.__self__.handlers  # bound method of an OpenerDirector
    assert not any(getattr(h, "proxies", None) for h in opener.__self__.handlers)
