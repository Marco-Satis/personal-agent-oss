"""HTTP server: local escalation endpoint, the Twilio webhooks and remote approvals.

Endpoints:
  POST /escalate                          local, `Authorization: Bearer <PA_API_TOKEN>`
  POST /twilio/voice                      Twilio webhook, call start (signature checked)
  POST /twilio/gather                     Twilio webhook, caller input (signature checked)
  GET  /approve/<id>                      approval page behind the one-time link (public)
  POST /approve/<id>                      approve or deny (CSRF token, origin checks)
  POST /internal/approvals                local, bearer: register a remote approval
  GET  /internal/approvals/<id>?wait=N    local, bearer: state of an approval (long poll)
  POST /internal/approvals/<id>/cancel    local, bearer: the agent stopped waiting
  GET  /healthz                           liveness

Bind to 127.0.0.1 and put a reverse proxy or tunnel in front that forwards only
`/twilio/` and `/approve/` (with the trailing slash) to the outside. `/escalate`
and `/internal/` must never be forwarded. As defense in depth, both answer 404
unless the client is on loopback (or in PA_TRUSTED_CLIENTS, e.g. a container's
bridge gateway) and the request carries no forwarding header, because many
tunnel tools forward the whole port when no path is given.

With PA_BRIEF_TIME set, a separate thread sends the morning brief once per day
(see brief.BriefScheduler). Notices (brief, alerts, task counts, info) go through
one notification layer (notify.Notifier, PA_NOTIFY_<KIND>); an alert is sent after
every escalation decision, asynchronously, so /escalate never waits for it.

With PA_TELEGRAM_TOKEN set and the bot paired (`personal-agent telegram pair`),
serve also runs the Telegram assistant (telegram_bot.TelegramService): long polling
only, outbound HTTPS, no inbound port. Its approvals and console approvals share
one inbox. SIGTERM shuts down cleanly (threads joined, Claude Code processes stopped).
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import signal
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

from .config import Config
from .conversation import Call, CallRegistry
from .escalation import EscalationPolicy, Event
from .approval import CONFIRM_WORD
from .notify import Notifier, alert_notice, alert_outcome, resolve_route
from .remote import ID_RE, ORIGIN_RE, TEXTS, ApprovalInbox, DiscordWebhook, render_message, render_page
from .status import read_statuses
from .telephony import CallProvider, FakeProvider, TwilioProvider, render_twiml, valid_twilio_signature

if TYPE_CHECKING:
    from .brief import BriefScheduler
    from .telegram_bot import TelegramService

log = logging.getLogger(__name__)

MAX_BODY = 8 * 1024
HANGUP_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'
FORWARDING_HEADERS = ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-real-ip")
APPROVE_PREFIX = "/approve/"
INTERNAL_PREFIX = "/internal/approvals"
PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    # same-origin, not no-referrer: with no-referrer browsers send `Origin: null` on the form POST.
    # The page loads nothing and links nowhere, so the one-time URL never leaves the origin.
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
    "frame-ancestors 'none'",
}


def _page_text(language: str, key: str) -> str:
    return TEXTS.get(language, TEXTS["en"])[key]


class Agent:
    """Wires policy, provider, call registry and status together."""

    def __init__(
        self, config: Config, provider: CallProvider | None = None, approvals: ApprovalInbox | None = None,
        notifier: Notifier | None = None, telegram: "TelegramService | None" = None,
    ) -> None:
        self.config = config
        self.notifier = notifier
        self.telegram = telegram
        if approvals is None and config.remote_approvals:
            approvals = ApprovalInbox(
                DiscordWebhook(config.discord_webhook) if config.discord_webhook else None,
                config.public_approval_url,
                config.language,
                config.approval_timeout_s,
                pin=config.approval_pin,
                route=self.approval_route,
                telegram=telegram.approval_sender if telegram is not None else None,
                on_final=telegram.on_final if telegram is not None else None,
                state_dir=config.state_dir,
            )
        self.approvals = approvals
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

    def approval_route(self) -> str | None:
        """The channel of a console approval right now (PA_NOTIFY_APPROVAL or the default)."""
        if self.telegram is not None:
            ready = self.telegram.target.ready() is None and not self.telegram.hijacked
        else:
            ready = self.notifier.telegram_ready() if self.notifier is not None else False
        route = resolve_route(self.config, "approval", ready)
        return route[0] if route else None

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
        result = self._decide_and_call(event)
        self._alert(event, result)
        return result

    def _decide_and_call(self, event: Event) -> dict:
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

    def _alert(self, event: Event, result: dict) -> None:
        """The alert notice (PA_NOTIFY_ALERT): queued, deduplicated per key; never affects the call."""
        if self.notifier is None:
            return
        try:
            outcome = alert_outcome(bool(result.get("called")), str(result.get("reason", "")), self.config.language)
            self.notifier.send("alert", alert_notice(event, outcome, self.config.language), key=event.key)
        except Exception as exc:  # noqa: BLE001 - a notice never changes the call decision
            log.warning("alert notice failed: %s", type(exc).__name__)

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
    timeout = 15  # public clients on /approve/ and /twilio/ must not hold a thread forever

    def log_message(self, fmt: str, *args) -> None:  # route through logging, without paths or queries
        path = self.path.split("?", 1)[0]
        if path.startswith(APPROVE_PREFIX):
            path = APPROVE_PREFIX + "…"  # the one-time id is a capability
        elif path.startswith(INTERNAL_PREFIX + "/"):
            path = INTERNAL_PREFIX + "/…" + ("/cancel" if path.endswith("/cancel") else "")
        log.debug("%s %s", self.command, path)

    def _send(self, code: int, body: str, content_type: str, headers: dict[str, str] | None = None) -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload), "application/json")

    def _html(self, code: int, body: str) -> None:
        self._send(code, body, "text/html; charset=utf-8", PAGE_HEADERS)

    def _read_body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length < 0 or length > MAX_BODY:
            return None
        return self.rfile.read(length)

    # --- access checks ---

    def _authorized(self) -> bool:
        token = self.agent.config.api_token or ""
        auth = self.headers.get("Authorization", "")
        return bool(token) and hmac.compare_digest(auth.encode(), f"Bearer {token}".encode())

    def _local(self) -> bool:
        """Trusted client (loopback or PA_TRUSTED_CLIENTS) and no forwarding header."""
        try:
            address = ipaddress.ip_address(str(self.client_address[0]).split("%", 1)[0])
        except (ValueError, IndexError):
            return False
        mapped = getattr(address, "ipv4_mapped", None)
        if mapped is not None:
            address = mapped
        trusted = address.is_loopback or any(
            address.version == network.version and address in network
            for network in self.agent.config.trusted_clients
        )
        if not trusted:
            return False
        for name in self.headers.keys():
            lowered = name.lower()
            if lowered in FORWARDING_HEADERS or lowered.startswith("tailscale-"):
                return False
        return True

    # --- routing ---

    def do_GET(self) -> None:
        parts = urllib.parse.urlsplit(self.path)
        path = parts.path
        if path == "/healthz":
            self._json(200, {"ok": True})
        elif path.startswith(APPROVE_PREFIX):
            self._approve_page(path[len(APPROVE_PREFIX):])
        elif path.startswith(INTERNAL_PREFIX + "/"):
            self._internal_state(path[len(INTERNAL_PREFIX) + 1:], parts.query)
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
        elif path.startswith(APPROVE_PREFIX):
            self._approve_post(path[len(APPROVE_PREFIX):], body)
        elif path == INTERNAL_PREFIX:
            self._internal_register(body)
        elif path.startswith(INTERNAL_PREFIX + "/") and path.endswith("/cancel"):
            self._internal_cancel(path[len(INTERNAL_PREFIX) + 1:-len("/cancel")])
        else:
            self._json(404, {"error": "not found"})

    def _escalate(self, body: bytes) -> None:
        if not self._local():
            self._json(404, {"error": "not found"})
            return
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return
        try:
            event = Event.from_dict(json.loads(body.decode() or "{}"))
        except (ValueError, UnicodeDecodeError) as exc:
            self._json(400, {"error": str(exc)})
            return
        self._json(200, self.agent.escalate(event))

    # --- remote approvals: local API for the chat process ---

    def _internal_guard(self) -> ApprovalInbox | None:
        """The inbox if the caller may use the internal API; otherwise the reply is already sent."""
        if not self._local():
            self._json(404, {"error": "not found"})
            return None
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return None
        if self.agent.approvals is None:
            self._json(503, {"error": "remote approvals not configured"})
            return None
        return self.agent.approvals

    def _internal_register(self, body: bytes) -> None:
        inbox = self._internal_guard()
        if inbox is None:
            return
        try:
            data = json.loads(body.decode() or "{}")
        except (ValueError, UnicodeDecodeError):
            data = None
        if not isinstance(data, dict):
            self._json(400, {"error": "invalid request"})
            return
        level, timeout_s = data.get("level"), data.get("timeout_s")
        tool_name, summary = data.get("tool_name"), data.get("summary")
        origin, context = data.get("origin", "chat"), data.get("context", False)
        if (
            type(level) is not int or level not in (2, 3)
            or type(timeout_s) is not int or not 1 <= timeout_s <= 3600
            or not isinstance(tool_name, str) or not tool_name or len(tool_name) > 200
            or not isinstance(summary, str) or len(summary) > 2000
            or not isinstance(origin, str) or not ORIGIN_RE.fullmatch(origin)
            or type(context) is not bool
        ):
            self._json(400, {"error": "invalid request"})
            return
        entry = inbox.register(tool_name, level, summary, timeout_s, origin=origin, context=context)
        if entry is None:
            self._json(429, {"error": "not available"})
            return
        self._json(200, {"id": entry.id, "via": entry.via})

    def _internal_state(self, approval_id: str, query: str) -> None:
        inbox = self._internal_guard()
        if inbox is None:
            return
        if not ID_RE.fullmatch(approval_id):
            self._json(404, {"error": "not found"})
            return
        try:
            raw = (urllib.parse.parse_qs(query, max_num_fields=5).get("wait") or ["0"])[0]
            wait = max(0, min(int(raw), 30))
        except ValueError:
            wait = 0
        state = inbox.wait(approval_id, wait)
        if state is None:
            self._json(404, {"error": "not found"})
            return
        self._json(200, {"state": state})

    def _internal_cancel(self, approval_id: str) -> None:
        inbox = self._internal_guard()
        if inbox is None:
            return
        if not ID_RE.fullmatch(approval_id) or not inbox.cancel(approval_id):
            self._json(404, {"error": "not found"})
            return
        self._json(200, {"cancelled": True})

    # --- remote approvals: the public page ---

    def _language(self) -> str:
        return self.agent.config.language

    def _not_found_page(self) -> None:
        # One generic answer for unknown, malformed, expired and used ids: no oracle.
        self._html(404, render_message(_page_text(self._language(), "invalid"), self._language()))

    def _approve_page(self, approval_id: str) -> None:
        inbox = self.agent.approvals
        entry = inbox.get(approval_id) if inbox is not None and ID_RE.fullmatch(approval_id) else None
        if entry is None:
            self._not_found_page()
            return
        self._html(200, render_page(entry, self._language()))

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is not None:
            public = urllib.parse.urlsplit(self.agent.config.public_approval_url or "")
            expected = f"{public.scheme}://{public.netloc}".lower()
            if not public.netloc or origin.strip().lower() != expected:
                return False
        site = self.headers.get("Sec-Fetch-Site")
        return site is None or site.strip().lower() in ("same-origin", "none")

    def _approve_post(self, approval_id: str, body: bytes) -> None:
        inbox = self.agent.approvals
        language = self._language()
        if inbox is None or not ID_RE.fullmatch(approval_id):
            self._not_found_page()
            return
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("application/x-www-form-urlencoded"):
            self._html(415, render_message(_page_text(language, "rejected"), language))
            return
        if not self._origin_ok():
            self._html(403, render_message(_page_text(language, "rejected"), language))
            return
        try:
            form = urllib.parse.parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=10)
        except (ValueError, UnicodeDecodeError):
            self._html(400, render_message(_page_text(language, "rejected"), language))
            return

        def field(name: str) -> str:
            values = form.get(name) or [""]
            return values[0]

        decision = field("decision")
        if decision not in ("allow", "deny"):
            self._html(400, render_message(_page_text(language, "rejected"), language))
            return
        outcome = inbox.resolve(approval_id, field("csrf"), decision, field("confirm"), field("pin"))
        if outcome == "ok":
            self._html(200, render_message(_page_text(language, "approved" if decision == "allow" else "denied"), language))
        elif outcome in ("need_confirm", "bad_pin"):
            entry = inbox.get(approval_id)
            if entry is None:
                self._not_found_page()
                return
            if outcome == "need_confirm":
                error = _page_text(language, "need_confirm").format(word=CONFIRM_WORD)
            else:
                error = _page_text(language, "bad_pin").format(left=inbox.pin_attempts_left(entry))
            self._html(200, render_page(entry, language, error))
        elif outcome == "locked":
            self._html(403, render_message(_page_text(language, "locked"), language))
        elif outcome == "bad_csrf":
            self._html(403, render_message(_page_text(language, "rejected"), language))
        elif outcome == "bad_request":
            self._html(400, render_message(_page_text(language, "rejected"), language))
        else:
            self._not_found_page()

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


def _brief_loop(scheduler: "BriefScheduler", stop: threading.Event, interval_s: float = 30) -> None:
    """Tick the morning brief scheduler until stop is set. A failed tick never ends the loop."""
    while not stop.is_set():
        try:
            scheduler.tick()
        except Exception as exc:  # noqa: BLE001 - log the type only, keep scheduling
            log.error("morning brief tick failed: %s", type(exc).__name__)
        if stop.wait(interval_s):
            break


def start_brief_thread(config: Config, stop: threading.Event,
                       notifier: Notifier | None = None) -> threading.Thread | None:
    """Start the daily brief in its own thread (separate from the session watch), or None when off.

    Local time comes from the process (TZ). The webhook is never logged.
    """
    if config.brief_time is None:
        return None
    from .brief import BriefScheduler

    thread = threading.Thread(target=_brief_loop, args=(BriefScheduler(config, notifier=notifier), stop),
                              daemon=True, name="morning-brief")
    thread.start()
    hour, minute = config.brief_time
    log.info("morning brief daily at %02d:%02d (grace %d min)", hour, minute, config.brief_grace_min)
    return thread


def serve(config: Config) -> None:
    config.validate_for_serve()
    config.state_dir.mkdir(parents=True, exist_ok=True)
    Path(config.status_dir).mkdir(parents=True, exist_ok=True)
    if config.bind_host not in ("127.0.0.1", "::1", "localhost"):
        log.warning(
            "binding to %s: make sure /escalate and /internal/ are not reachable from outside", config.bind_host
        )
    from .telegram import install_excepthooks, install_redaction

    install_redaction(config)
    install_excepthooks()
    telegram: "TelegramService | None" = None
    if config.telegram_token:
        from .telegram_bot import TelegramService

        notifier = Notifier.from_config(config)
        telegram = TelegramService(config, notifier)
        notifier.targets["telegram"] = telegram.target  # one client, one cooldown and pacing state
    else:
        notifier = Notifier.from_config(config)
    agent = Agent(config, notifier=notifier, telegram=telegram)
    if agent.approvals is not None:
        log.info("remote approvals enabled: forward only /approve/ (never /internal/) to your phone")
    server = make_server(agent)
    stop = threading.Event()
    watcher = threading.Thread(target=_watch_loop, args=(agent, stop), daemon=True, name="session-watch")
    watcher.start()
    start_brief_thread(config, stop, notifier)
    if telegram is not None:
        telegram.start(agent.approvals)
        log.info("telegram enabled: the assistant polls once the bot is paired (personal-agent telegram pair)")

    def on_sigterm(_signum: int, _frame: object) -> None:
        # serve_forever runs in this (main) thread: shutdown() must come from another one.
        threading.Thread(target=server.shutdown, name="shutdown", daemon=True).start()

    previous = None
    if threading.current_thread() is threading.main_thread():
        previous = signal.signal(signal.SIGTERM, on_sigterm)
    log.info("listening on %s:%s (provider: %s)", config.bind_host, config.port, config.provider)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if telegram is not None:
            telegram.close(join_s=8.0)
        notifier.close(5.0)
        server.server_close()
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
