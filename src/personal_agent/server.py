"""HTTP server: local escalation endpoint plus the Twilio webhooks.

Endpoints:
  POST /escalate        local callers only, needs `Authorization: Bearer <PA_API_TOKEN>`
  POST /twilio/voice    Twilio webhook, call start (signature checked)
  POST /twilio/gather   Twilio webhook, caller input (signature checked)
  GET  /healthz         liveness

Bind to 127.0.0.1 and put a reverse proxy or tunnel in front that forwards
only `/twilio/` to the internet. `/escalate` must never be public.
"""

from __future__ import annotations

import hmac
import json
import logging
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import Config
from .conversation import Call, CallRegistry
from .escalation import EscalationPolicy, Event
from .status import read_statuses
from .telephony import CallProvider, FakeProvider, TwilioProvider, render_twiml, valid_twilio_signature

log = logging.getLogger(__name__)

MAX_BODY = 8 * 1024
HANGUP_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'


class Agent:
    """Wires policy, provider, call registry and status together."""

    def __init__(self, config: Config, provider: CallProvider | None = None) -> None:
        self.config = config
        self.policy = EscalationPolicy(
            config.state_dir / "escalation.json",
            config.max_calls_per_day,
            config.min_call_interval_min,
            config.quiet_hours,
        )
        self.calls = CallRegistry()
        if provider is not None:
            self.provider = provider
        elif config.provider == "twilio":
            assert config.twilio is not None and config.public_url
            self.provider = TwilioProvider(config.twilio, config.public_url)
        else:
            self.provider = FakeProvider()

    def statuses(self):
        return read_statuses(self.config.status_dir, self.config.status_max_age_min)

    def new_call(self, event: Event) -> Call:
        assert self.config.call_pin
        return Call(
            pin=self.config.call_pin,
            event_key=event.key,
            event_summary=event.summary,
            event_source=event.source,
            read_statuses=self.statuses,
            acknowledge=self.policy.acknowledge,
            language=self.config.language,
        )

    def escalate(self, event: Event) -> dict:
        decision = self.policy.decide(event)
        log.info("event %s (%s): call=%s, %s", event.key, event.severity, decision.call, decision.reason)
        if not decision.call:
            return {"called": False, "reason": decision.reason}
        call = self.new_call(event)
        self.calls.add(call)
        assert self.config.call_to
        try:
            self.provider.place_call(self.config.call_to, call.call_id)
        except Exception as exc:  # provider errors must not crash the server
            log.error("placing the call failed: %s", type(exc).__name__)
            return {"called": False, "reason": "provider error"}
        return {"called": True, "reason": decision.reason}

    def check_waiting_sessions(self) -> None:
        """Escalate sessions that wait for input longer than configured."""
        limit = self.config.waiting_escalate_min
        if limit <= 0:
            return
        for status in self.statuses():
            if status.state == "waiting" and status.age_minutes() >= limit:
                self.escalate(
                    Event(
                        key=f"waiting:{status.session_id}",
                        severity="critical",
                        summary=f"{status.project} waits for input since {status.age_minutes()} minutes.",
                        source="session watch",
                    )
                )


class Handler(BaseHTTPRequestHandler):
    agent: Agent  # set by make_server
    server_version = "personal-agent"
    sys_version = ""

    def log_message(self, fmt: str, *args) -> None:  # route through logging, without query strings
        log.debug("%s %s", self.command, self.path.split("?", 1)[0])

    def _send(self, code: int, body: str, content_type: str) -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload), "application/json")

    def _read_body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length < 0 or length > MAX_BODY:
            return None
        return self.rfile.read(length)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        body = self._read_body()
        if body is None:
            self._json(413, {"error": "body too large or invalid"})
            return
        if path == "/escalate":
            self._escalate(body)
        elif path in ("/twilio/voice", "/twilio/gather"):
            self._twilio(path, body)
        else:
            self._json(404, {"error": "not found"})

    def _escalate(self, body: bytes) -> None:
        token = self.agent.config.api_token or ""
        auth = self.headers.get("Authorization", "")
        if not token or not hmac.compare_digest(auth.encode(), f"Bearer {token}".encode()):
            self._json(401, {"error": "unauthorized"})
            return
        try:
            event = Event.from_dict(json.loads(body.decode() or "{}"))
        except (ValueError, UnicodeDecodeError) as exc:
            self._json(400, {"error": str(exc)})
            return
        self._json(200, self.agent.escalate(event))

    def _twilio(self, path: str, body: bytes) -> None:
        config = self.agent.config
        params = {k: v[0] for k, v in urllib.parse.parse_qs(body.decode(errors="replace")).items()}
        if config.provider == "twilio":
            assert config.twilio is not None
            url = f"{config.public_url}{self.path}"
            if not valid_twilio_signature(
                config.twilio.auth_token, url, params, self.headers.get("X-Twilio-Signature")
            ):
                log.warning("rejected webhook with invalid signature")
                self._send(403, HANGUP_TWIML, "text/xml")
                return
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        call_id = (query.get("cid") or [None])[0]
        call = self.agent.calls.get(call_id)
        if call is None:
            self._send(200, HANGUP_TWIML, "text/xml")
            return
        if path == "/twilio/voice":
            turn = call.start()
        else:
            turn = call.handle(digits=params.get("Digits"), speech=params.get("SpeechResult"))
        action = f"{config.public_url or ''}/twilio/gather?{urllib.parse.urlencode({'cid': call.call_id})}"
        self._send(200, render_twiml(turn, action), "text/xml")


def make_server(agent: Agent) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"agent": agent})
    return ThreadingHTTPServer((agent.config.bind_host, agent.config.port), handler)


def _watch_loop(agent: Agent, stop: threading.Event, interval_s: int = 60) -> None:
    while not stop.wait(interval_s):
        try:
            agent.check_waiting_sessions()
        except Exception as exc:  # keep watching even if one round fails
            log.error("session watch failed: %s", type(exc).__name__)


def serve(config: Config) -> None:
    config.validate_for_serve()
    config.state_dir.mkdir(parents=True, exist_ok=True)
    Path(config.status_dir).mkdir(parents=True, exist_ok=True)
    if config.bind_host not in ("127.0.0.1", "::1", "localhost"):
        log.warning("binding to %s: make sure /escalate is not reachable from outside", config.bind_host)
    agent = Agent(config)
    server = make_server(agent)
    stop = threading.Event()
    watcher = threading.Thread(target=_watch_loop, args=(agent, stop), daemon=True)
    watcher.start()
    log.info("listening on %s:%s (provider: %s)", config.bind_host, config.port, config.provider)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
