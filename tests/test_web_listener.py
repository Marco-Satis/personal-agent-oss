"""L1-1: the web listener on its own port (design gates G1, G3, G4 headers, G6 route table)."""

import dataclasses
import ipaddress
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

import pytest

from personal_agent.config import Config, ConfigError
from personal_agent.server import Agent
from personal_agent.telephony import FakeProvider
from personal_agent.web.listener import (
    APP_CSP, BoundedThreadingHTTPServer, MAX_API_BODY, client_ip, make_web_server, static_file,
)
from personal_agent.web.routes import ROUTES, resolve

ORIGIN = "https://agent.example.org:8788"
LOOPBACK = (ipaddress.ip_network("127.0.0.1/32"), ipaddress.ip_network("::1/128"))


def config(tmp_path, **extra):
    return Config(state_dir=tmp_path / "state", status_dir=tmp_path / "sessions", port=0, web_port=0,
                  web_enabled=True, web_origin=ORIGIN, trusted_proxies=LOOPBACK, api_token="t" * 32, **extra)


@pytest.fixture
def web(tmp_path):
    agent = Agent(config(tmp_path), provider=FakeProvider())
    server = make_web_server(agent)
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    yield server, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def call(url, method="GET", data=None, headers=None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.headers, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode()


# --- the route table ------------------------------------------------------------------------------------


def test_route_table_is_exactly_design_gate_g6():
    assert {(r.method, r.path) for r in ROUTES} == {
        ("GET", "/healthz"), ("GET", "/api/session"),
        ("POST", "/api/login/password"), ("POST", "/api/login/totp"), ("POST", "/api/login/passkey/begin"),
        ("POST", "/api/login/passkey/finish"), ("POST", "/api/recovery"),
        ("POST", "/api/logout"), ("POST", "/api/logout-all"),
        ("GET", "/api/today"), ("GET", "/api/approvals"), ("POST", "/api/approvals/<id>"), ("GET", "/api/system"),
        ("GET", "/api/settings"), ("PUT", "/api/settings"),
        ("PUT", "/api/secrets/<name>"), ("DELETE", "/api/secrets/<name>"),
        ("GET", "/api/mail/accounts"), ("PUT", "/api/mail/accounts"), ("DELETE", "/api/mail/accounts"),
        ("POST", "/api/mail/test"), ("POST", "/api/mail/test-draft"),
        ("POST", "/api/telegram/pair"), ("POST", "/api/telegram/unpair"),
        ("POST", "/api/push/subscribe"), ("DELETE", "/api/push/subscribe"),
        ("GET", "/api/passkeys"), ("POST", "/api/passkeys"), ("DELETE", "/api/passkeys"),
        ("GET", "/api/totp"), ("POST", "/api/totp"), ("DELETE", "/api/totp"),
        ("GET", "/api/events"), ("GET", "/app/*"),
        ("GET", "/approve/<id>"), ("POST", "/approve/<id>"), ("POST", "/twilio/voice"), ("POST", "/twilio/gather"),
        ("POST", "/chat"), ("GET", "/chat/<id>"),
    }
    assert not [r for r in ROUTES if r.path.startswith(("/internal", "/escalate"))]


def test_resolve_matches_ids_and_reports_allowed_methods():
    route, params, _ = resolve("POST", "/api/approvals/abc_DEF-123")
    assert route.name == "approval_decide" and params == {"id": "abc_DEF-123"}
    assert resolve("POST", "/api/approvals/a.b")[0] is None  # ids are one plain segment
    assert resolve("POST", "/api/approvals/a/b")[0] is None
    assert resolve("DELETE", "/healthz") == (None, {}, ("GET",))
    assert resolve("GET", "/app/css/site.css")[1] == {"rest": "css/site.css"}


@pytest.mark.parametrize("path", ["/internal/approvals", "/internal/approvals/" + "a" * 32,
                                  "/internal/approvals/" + "a" * 32 + "/cancel", "/escalate"])
def test_internal_routes_do_not_exist_on_the_web_port(web, path):
    _, base = web
    auth = {"Authorization": "Bearer " + "t" * 32, "Content-Type": "application/json"}  # the right token, from loopback
    assert call(base + path, "POST", b"{}", auth)[0] == 404
    assert call(base + path, headers=auth)[0] == 404


@pytest.mark.parametrize("route", [r for r in ROUTES if r.access in ("session", "confirm")],
                         ids=lambda r: f"{r.method} {r.path}")
def test_every_session_route_answers_401_without_sign_in(web, route):
    _, base = web
    path = route.path.replace("<id>", "x1").replace("<name>", "telegram")
    data = b"{}" if route.method != "GET" else None
    code, headers, body = call(base + path, route.method, data, {"Content-Type": "application/json"})
    assert code == 401 and json.loads(body) == {"error": "unauthorized"}
    assert headers["Cache-Control"] == "no-store"


def test_public_routes_unknown_paths_and_wrong_methods(web):
    _, base = web
    assert call(base + "/healthz")[:3:2] == (200, '{"ok": true}')
    assert json.loads(call(base + "/api/session")[2]) == {"authenticated": False}
    assert call(base + "/api/login/password", "POST", b"{}")[0] == 501  # sign-in arrives with L1-3
    assert call(base + "/nope")[0] == 404
    assert call(base + "/api")[0] == 404
    code, headers, _ = call(base + "/healthz", "DELETE")
    assert code == 405 and headers["Allow"] == "GET"
    assert call(base + "/api/session", "POST", b"")[0] == 405


# --- headers and limits ---------------------------------------------------------------------------------


def _security_headers_ok(headers, csp=APP_CSP):
    assert headers.get_all("Content-Security-Policy") == [csp]
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "same-origin"
    assert headers["Cross-Origin-Opener-Policy"] == "same-origin"
    assert headers["Permissions-Policy"] == "microphone=(self)"
    assert headers["Strict-Transport-Security"] == "max-age=31536000"
    return True


def test_security_headers_on_every_answer(web):
    _, base = web
    for code, headers, _ in (call(base + "/healthz"), call(base + "/api/today"), call(base + "/nope"),
                             call(base + "/healthz", "DELETE"),
                             call(base + "/api/login/totp", "POST", b"x" * (MAX_API_BODY + 1))):
        assert code in (200, 401, 404, 405, 413) and _security_headers_ok(headers)
    # the approval page keeps its own, stricter policy instead of a second one
    code, headers, body = call(base + "/approve/" + "a" * 32)
    assert code == 404 and headers["Content-Type"].startswith("text/html")
    assert len(headers.get_all("Content-Security-Policy")) == 1
    assert headers["Content-Security-Policy"].startswith("default-src 'none'")


def test_no_hsts_for_a_local_http_origin(tmp_path):
    agent = Agent(dataclasses.replace(config(tmp_path), web_origin="http://127.0.0.1:8788"), provider=FakeProvider())
    server = make_web_server(agent)
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    try:
        headers = call(f"http://127.0.0.1:{server.server_address[1]}/healthz")[1]
        assert "Strict-Transport-Security" not in headers
    finally:
        server.shutdown()
        server.server_close()


def test_body_limits_per_route_family(web):
    _, base = web
    assert call(base + "/api/login/password", "POST", b"x" * 9000)[0] == 501  # API: up to 64 KB
    assert call(base + "/api/login/password", "POST", b"x" * (MAX_API_BODY + 1))[0] == 413
    assert call(base + "/chat", "POST", b"x" * 9000)[0] == 413  # existing routes keep 8 KB


# --- client address -------------------------------------------------------------------------------------


@pytest.mark.parametrize("peer,xff,expected", [
    ("100.64.0.9", "203.0.113.9", "100.64.0.9"),            # not a proxy: its header counts for nothing
    ("127.0.0.1", None, "127.0.0.1"),
    ("127.0.0.1", "100.76.0.11", "100.76.0.11"),            # tailscale serve: exactly one entry (measured 24.11.)
    ("127.0.0.1", "198.51.100.1, 100.76.0.11", "100.76.0.11"),  # rightmost non-proxy, never the spoofable left
    ("127.0.0.1", "203.0.113.9, 127.0.0.1", "203.0.113.9"),
    ("127.0.0.1", "nonsense, 100.76.0.11", "100.76.0.11"),
    ("127.0.0.1", "100.76.0.11, nonsense", "127.0.0.1"),    # garbage: stay with the proxy itself
    ("::1", "2001:db8::5", "2001:db8::5"),
    ("::ffff:127.0.0.1", "100.76.0.11", "100.76.0.11"),
    ("bad", "100.76.0.11", None),
])
def test_client_ip_trusts_only_proxies(peer, xff, expected):
    result = client_ip(peer, xff, LOOPBACK)
    assert (str(result) if result is not None else None) == expected


# --- static app shell -----------------------------------------------------------------------------------


def test_static_files_stay_inside_dist(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html>")
    (dist / "assets" / "app.js").write_text("1")
    (dist / ".env").write_text("SECRET=1")
    (dist / "notes.txt").write_text("x")
    (tmp_path / "outside.js").write_text("x")
    os.symlink(tmp_path / "outside.js", dist / "link.js")
    assert static_file(dist, "") == dist.resolve() / "index.html"
    assert static_file(dist, "assets/app.js") == dist.resolve() / "assets" / "app.js"
    for rest in ("../outside.js", "assets/../../outside.js", ".env", "notes.txt", "link.js", "assets//app.js",
                 "missing.js", "assets"):
        assert static_file(dist, rest) is None, rest


def test_app_shell_is_served_with_its_type(web, tmp_path):
    server, base = web
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>x</title>")
    server.RequestHandlerClass.dist_dir = dist
    code, headers, body = call(base + "/app/")
    assert code == 200 and body.startswith("<!doctype html>")
    assert headers["Content-Type"] == "text/html; charset=utf-8" and headers["Cache-Control"] == "no-cache"
    assert _security_headers_ok(headers)
    assert call(base + "/app/missing.js")[0] == 404


# --- thread limit ---------------------------------------------------------------------------------------


def test_requests_beyond_the_thread_limit_get_503_at_once():
    entered, gate = threading.Event(), threading.Event()

    class Slow(BaseHTTPRequestHandler):
        def do_GET(self):
            entered.set()
            gate.wait(5)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    server = BoundedThreadingHTTPServer(("127.0.0.1", 0), Slow, max_threads=1)
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    first = {}
    t = threading.Thread(target=lambda: first.setdefault("code", call(base + "/")[0]))
    try:
        t.start()
        assert entered.wait(5)
        code, headers, body = call(base + "/")
        assert code == 503 and json.loads(body) == {"error": "busy"} and headers["Retry-After"] == "5"
        gate.set()
        t.join(5)
        assert first["code"] == 200
        deadline = time.monotonic() + 2
        while call(base + "/")[0] != 200:  # the slot comes back once the first thread has ended
            assert time.monotonic() < deadline
            time.sleep(0.02)
    finally:
        gate.set()
        server.shutdown()
        server.server_close()


def test_a_client_that_sends_nothing_does_not_block_the_others(web):
    _, base = web
    host, port = base[len("http://"):].split(":")
    idle = socket.create_connection((host, int(port)))
    try:
        assert call(base + "/healthz")[0] == 200
    finally:
        idle.close()


# --- configuration --------------------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PA_STATE_DIR", str(tmp_path / "state"))
    for name in ("PA_WEB", "PA_WEB_PORT", "PA_WEB_ORIGIN", "PA_TRUSTED_PROXIES", "PA_PORT"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_web_is_off_by_default_and_trusts_only_loopback_proxies(env):
    c = Config.from_env()
    assert not c.web_enabled and c.web_port == 8788 and c.web_origin is None
    assert [str(n) for n in c.trusted_proxies] == ["127.0.0.1/32", "::1/128"]
    c.validate_for_serve()


@pytest.mark.parametrize("raw,expected", [
    ("https://agent.tail1234.ts.net:8788", "https://agent.tail1234.ts.net:8788"),
    ("https://Agent.Example.org/", "https://agent.example.org"),
    ("http://127.0.0.1:8788", "http://127.0.0.1:8788"),
    ("http://[::1]:8788", "http://[::1]:8788"),
])
def test_web_origin_is_normalised(env, raw, expected):
    env.setenv("PA_WEB_ORIGIN", raw)
    assert Config.from_env().web_origin == expected


@pytest.mark.parametrize("raw", ["http://agent.example.org", "https://agent.example.org/app", "https://u@agent.example.org",
                                 "https://agent.example.org?x=1", "ftp://agent.example.org", "agent.example.org",
                                 "https://agent.example.org:99999"])
def test_bad_web_origins_are_refused(env, raw):
    env.setenv("PA_WEB_ORIGIN", raw)
    with pytest.raises(ConfigError, match="PA_WEB_ORIGIN"):
        Config.from_env()


def test_web_needs_an_origin_and_its_own_port(env):
    env.setenv("PA_WEB", "on")
    with pytest.raises(ConfigError, match="PA_WEB_ORIGIN"):
        Config.from_env().validate_for_serve()
    env.setenv("PA_WEB_ORIGIN", ORIGIN)
    env.setenv("PA_WEB_PORT", "8787")
    with pytest.raises(ConfigError, match="differ"):
        Config.from_env().validate_for_serve()
    env.setenv("PA_TRUSTED_PROXIES", "0.0.0.0/0")
    with pytest.raises(ConfigError, match="PA_TRUSTED_PROXIES"):
        Config.from_env()
