"""The web listener (PA_WEB=on): the app's own port next to the internal one (design gate G1).

The internal listener (PA_PORT, 8787) keeps /internal/... and /escalate for local processes.
This one (PA_WEB_PORT, 8788) serves only the rows of `routes.ROUTES`: the app, its API and the
existing public routes /approve/, /twilio/ and /chat with their own checks. A route that is not in
the table does not exist here, so no proxy mistake can expose an internal route.

Both bind to 127.0.0.1; `tailscale serve` (stage A) or Caddy (stage B) sits in front. Measured on
24.11.2026: tailscale serve always connects from loopback and replaces X-Forwarded-For with exactly
the caller's tailnet address, which `client_ip` reads only from trusted proxies (PA_TRUSTED_PROXIES).

Every answer carries the security headers (CSP, nosniff, same-origin referrer and opener policy,
microphone only for the app itself, HSTS when the origin is https). At most MAX_THREADS requests run
at once; the next one gets 503 at once instead of a thread.

Sign-in arrives with L1-3: until then every route that needs a session answers 401.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import stat
import threading
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path

from ..server import MAX_BODY, Agent, Handler
from .routes import resolve

log = logging.getLogger(__name__)

MAX_API_BODY = 64 * 1024
MAX_THREADS = 32
MAX_STATIC_BYTES = 5 * 1024 * 1024
DIST_DIR = Path(__file__).parent / "dist"
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".webmanifest": "application/manifest+json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}
APP_CSP = ("default-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
           "base-uri 'none'; form-action 'self'")
BASE_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    # same-origin, not no-referrer: with no-referrer browsers send `Origin: null` on their own POSTs (G4)
    ("Referrer-Policy", "same-origin"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Permissions-Policy", "microphone=(self)"),
)
_BUSY_BODY = b'{"error":"busy"}'
BUSY_RESPONSE = (b"HTTP/1.0 503 Service Unavailable\r\nContent-Type: application/json\r\n"
                 b"Content-Length: " + str(len(_BUSY_BODY)).encode() + b"\r\nRetry-After: 5\r\n"
                 b"Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nConnection: close\r\n\r\n"
                 + _BUSY_BODY)

Network = ipaddress.IPv4Network | ipaddress.IPv6Network
Address = ipaddress.IPv4Address | ipaddress.IPv6Address


def _ip(raw: str) -> Address | None:
    try:
        address = ipaddress.ip_address(raw.strip().split("%", 1)[0])
    except ValueError:
        return None
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped if mapped is not None else address


def _trusted(address: Address, proxies: tuple[Network, ...]) -> bool:
    return any(address.version == net.version and address in net for net in proxies)


def client_ip(peer: str, forwarded_for: str | None, proxies: tuple[Network, ...]) -> Address | None:
    """The caller's address for lockouts and sign-in notices (G1).

    X-Forwarded-For counts only when the direct peer is a trusted proxy, and then its rightmost
    entry that is not a trusted proxy itself. A malformed entry ends the walk at the proxy's own
    address: a garbled header never lets a caller pick an address of its choice.
    """
    address = _ip(peer)
    if address is None or not forwarded_for or not _trusted(address, proxies):
        return address
    for part in reversed(forwarded_for.split(",")):
        hop = _ip(part)
        if hop is None:
            return address
        if not _trusted(hop, proxies):
            return hop
    return address


def static_file(dist: Path, rest: str) -> Path | None:
    """The file under `dist` for the URL rest, or None: no dot segments, no symlinks, known types only."""
    segments = (rest or "index.html").split("/")
    if any(not s or s.startswith(".") or "\\" in s or "\0" in s for s in segments):
        return None
    target = dist.joinpath(*segments)
    if target.suffix.lower() not in STATIC_TYPES:
        return None
    try:
        base = dist.resolve(strict=True)
        current = base
        for segment in segments:  # refuse a symlink anywhere below dist
            current = current / segment
            info = os.lstat(current)
            if stat.S_ISLNK(info.st_mode):
                return None
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_STATIC_BYTES:
        return None
    return current


class WebHandler(Handler):
    """Routes by `routes.ROUTES` only; reuses the checks of the existing public routes."""

    dist_dir: Path = DIST_DIR

    def setup(self) -> None:
        super().setup()
        self._sent_headers: set[str] = set()

    def send_header(self, keyword: str, value: str) -> None:
        self._sent_headers.add(keyword.lower())
        super().send_header(keyword, value)

    def end_headers(self) -> None:
        path = urllib.parse.urlsplit(getattr(self, "path", "") or "").path
        extra = list(BASE_HEADERS)
        if "content-security-policy" not in self._sent_headers:
            extra.append(("Content-Security-Policy", APP_CSP))  # the approval page brings its own, stricter one
        if path.startswith("/api/"):
            extra.append(("Cache-Control", "no-store"))
        if (self.agent.config.web_origin or "").startswith("https://"):
            extra.append(("Strict-Transport-Security", "max-age=31536000"))
        for name, value in extra:
            if name.lower() not in self._sent_headers:
                super().send_header(name, value)
        self._sent_headers = set()
        super().end_headers()

    def client_ip(self) -> Address | None:
        return client_ip(str(self.client_address[0]), self.headers.get("X-Forwarded-For"),
                         self.agent.config.trusted_proxies)

    def _read_body(self, limit: int = MAX_BODY) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length < 0 or length > limit:
            return None
        return self.rfile.read(length)

    # --- routing ---

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        parts = urllib.parse.urlsplit(self.path)
        route, params, allowed = resolve(method, parts.path)
        if route is None:
            if allowed:
                self._send(405, json.dumps({"error": "method not allowed"}), "application/json",
                           {"Allow": ", ".join(allowed)})
            else:
                self._json(404, {"error": "not found"})
            return
        if route.access in ("session", "confirm"):
            self._json(401, {"error": "unauthorized"})  # sign-in arrives with L1-3
            return
        body = b""
        if method != "GET":
            body = self._read_body(MAX_API_BODY if parts.path.startswith("/api/") else MAX_BODY)
            if body is None:
                self._json(413, {"error": "body too large or invalid"})
                return
        handler = getattr(self, "_web_" + route.name, None)
        if handler is None:
            self._json(501, {"error": "not available yet"})
            return
        handler(params, parts.query, body)

    # --- handlers ---

    def _web_health(self, params: dict, query: str, body: bytes) -> None:
        self._json(200, {"ok": True})

    def _web_session_state(self, params: dict, query: str, body: bytes) -> None:
        self._json(200, {"authenticated": False})

    def _web_app(self, params: dict, query: str, body: bytes) -> None:
        target = static_file(self.dist_dir, params["rest"])
        if target is None:
            self._json(404, {"error": "not found"})
            return
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", STATIC_TYPES[target.suffix.lower()])
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _web_approve_page(self, params: dict, query: str, body: bytes) -> None:
        self._approve_page(params["id"])

    def _web_approve_post(self, params: dict, query: str, body: bytes) -> None:
        self._approve_post(params["id"], body)

    def _web_twilio(self, params: dict, query: str, body: bytes) -> None:
        self._twilio(urllib.parse.urlsplit(self.path).path, body)

    def _web_chat_post(self, params: dict, query: str, body: bytes) -> None:
        self._chat_post(body)

    def _web_chat_result(self, params: dict, query: str, body: bytes) -> None:
        self._chat_result(params["id"], query)


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """A thread per request, but at most `max_threads` at once; beyond that 503 without a thread."""

    def __init__(self, address: tuple[str, int], handler: type, max_threads: int = MAX_THREADS) -> None:
        super().__init__(address, handler)
        self._slots = threading.BoundedSemaphore(max_threads)

    def process_request(self, request, client_address) -> None:  # noqa: ANN001 - socketserver signature
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(BUSY_RESPONSE)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:  # noqa: ANN001
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def make_web_server(agent: Agent, max_threads: int = MAX_THREADS) -> BoundedThreadingHTTPServer:
    handler = type("BoundWebHandler", (WebHandler,), {"agent": agent})
    return BoundedThreadingHTTPServer((agent.config.bind_host, agent.config.web_port), handler, max_threads)


def start_web_listener(agent: Agent) -> tuple[BoundedThreadingHTTPServer, threading.Thread]:
    server = make_web_server(agent)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="web-listener")
    thread.start()
    log.info("web listener on %s:%s for %s", agent.config.bind_host, agent.config.web_port, agent.config.web_origin)
    return server, thread
