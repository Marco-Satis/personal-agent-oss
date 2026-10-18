"""Approvals from the phone: a Discord webhook notice plus a one-time approval page.

Flow, when the chat runs with `--remote-approvals`:

1. The broker in the chat process hands a level-2/3 request to `RemoteChannel`.
2. `RemoteChannel` registers it with the running `serve` process over the local,
   bearer-protected `/internal/approvals` API (two processes cannot share the port).
3. `ApprovalInbox` in `serve` posts a short notice to the Discord webhook: tool name,
   level and a one-time link `<public base>/approve/<random id>`. No input content.
4. The person opens the link (served by `serve`, reachable only through the
   tailnet or proxy), sees the request and approves or denies with a CSRF-safe
   POST. Level 3 also needs the typed confirmation word and the approval PIN.
5. `RemoteChannel` polls the decision. Expiry, cancellation and every error mean deny.

Webhooks are write-only: nothing comes back through Discord. GET never changes
state, so link previews and prefetchers cannot approve anything.
"""

from __future__ import annotations

import hmac
import html
import json
import logging
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from . import __version__
from .approval import CONFIRM_WORD, CONTEXT_HINT, LEVEL_LABELS, ApprovalRequest, Level
from .status import clean_text

log = logging.getLogger(__name__)

ID_RE = re.compile(r"[A-Za-z0-9_-]{32}")
EXPIRY_MARGIN_S = 5          # the page expires before the agent's own deadline
MAX_PENDING = 5
MAX_PIN_ATTEMPTS = 3         # wrong PINs before the request is denied
NOTICE_LIMIT = 5             # webhook posts ...
NOTICE_WINDOW_S = 10 * 60    # ... per window
PRUNE_AFTER_S = 10 * 60      # finished entries are kept this long after expiry
MAX_WAIT_S = 30
STATES = ("pending", "allowed", "denied", "expired")
FINAL_STATES = ("allowed", "denied", "expired")

TEXTS = {
    "en": {
        "notice": "Approval needed (level {level}): {tool}. Open <{link}> (expires in {minutes} min).",
        "title": "Approval needed",
        "level": "Level {level} ({label})",
        "expires": "Expires at {time}",
        "confirm": 'Type "{word}" to allow',
        "pin": "Approval PIN",
        "allow": "Allow",
        "deny": "Deny",
        "need_confirm": 'Type "{word}" to allow this level-3 action.',
        "bad_pin": "Wrong PIN. {left} attempt(s) left.",
        "approved": "Approved. You can close this page.",
        "denied": "Denied. You can close this page.",
        "locked": "Too many wrong PINs. The request was denied.",
        "invalid": "This approval link is unknown, expired or already used.",
        "rejected": "Request rejected.",
    },
    "de": {
        "notice": "Freigabe nötig (Stufe {level}): {tool}. Öffnen: <{link}> (läuft in {minutes} Min. ab).",
        "title": "Freigabe nötig",
        "level": "Stufe {level} ({label})",
        "expires": "Läuft ab um {time}",
        "confirm": '"{word}" eingeben, um zu erlauben',
        "pin": "Freigabe-PIN",
        "allow": "Erlauben",
        "deny": "Ablehnen",
        "need_confirm": 'Für Stufe 3 bitte "{word}" eingeben.',
        "bad_pin": "Falsche PIN. Noch {left} Versuch(e).",
        "approved": "Erlaubt. Die Seite kann geschlossen werden.",
        "denied": "Abgelehnt. Die Seite kann geschlossen werden.",
        "locked": "Zu viele falsche PINs. Die Anfrage wurde abgelehnt.",
        "invalid": "Dieser Freigabe-Link ist unbekannt, abgelaufen oder schon benutzt.",
        "rejected": "Anfrage abgewiesen.",
    },
}


_DISCORD_MARKUP = re.compile(r"([\\_*~|`>#\[\]()-])")


def _texts(language: str) -> dict[str, str]:
    return TEXTS.get(language, TEXTS["en"])


def discord_safe(text: object, limit: int = 60) -> str:
    """Plain text for a Discord message: no mentions, no markup (`mcp__a__b` would underline)."""
    return _DISCORD_MARKUP.sub(r"\\\1", clean_text(text, limit))


def _close_quietly(exc: urllib.error.HTTPError) -> None:
    try:
        exc.close()
    except Exception:  # noqa: BLE001 - an HTTPError without a body cannot be closed
        pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects become errors: a secret URL or bearer token never follows one."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def local_opener() -> Callable:
    """Opener for calls to our own server: never through a proxy, never redirected.

    With HTTP_PROXY set and no_proxy unset, urllib would send 127.0.0.1 requests,
    bearer token included, to the proxy.
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect()).open


# --- Discord -------------------------------------------------------------------


class DiscordWebhook:
    """Posts a plain notice to a Discord webhook. The URL is a secret and never logged."""

    def __init__(self, url: str, timeout_s: int = 10, opener: Callable | None = None) -> None:
        self._url = url
        self.timeout_s = timeout_s
        self._opener = opener or urllib.request.build_opener(_NoRedirect()).open

    def __repr__(self) -> str:
        return "DiscordWebhook(<hidden>)"

    def post(self, text: str) -> bool:
        body = json.dumps(
            {"content": text[:1900], "allowed_mentions": {"parse": []}, "flags": 4}  # 4: no embeds
        ).encode()
        try:
            request = urllib.request.Request(self._url, data=body, method="POST")
            request.add_header("Content-Type", "application/json")
            request.add_header("User-Agent", f"personal-agent/{__version__}")
            with self._opener(request, timeout=self.timeout_s) as response:
                code = getattr(response, "status", None) or response.getcode()
        except urllib.error.HTTPError as exc:
            _close_quietly(exc)
            log.warning("webhook post failed: HTTPError %s", exc.code)
            return False
        except Exception as exc:  # noqa: BLE001 - messages of some errors contain the URL
            log.warning("webhook post failed: %s", type(exc).__name__)
            return False
        if not 200 <= int(code) < 300:
            log.warning("webhook post failed: HTTP %s", code)
            return False
        return True


# --- the inbox in the server process -------------------------------------------


@dataclass
class PendingApproval:
    id: str
    tool_name: str
    level: int
    summary: str
    expires_at: float
    csrf: str
    state: str = "pending"          # pending | allowed | denied | expired
    pin_attempts: int = 0
    done: threading.Event = field(default_factory=threading.Event)


class ApprovalInbox:
    """Pending remote approvals, each with a one-time id and a CSRF token."""

    def __init__(
        self,
        notifier: DiscordWebhook,
        public_base: str,
        language: str = "en",
        max_timeout_s: int = 300,
        pin: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.notifier = notifier
        self.public_base = public_base.rstrip("/")
        self.language = language
        self.max_timeout_s = max_timeout_s
        self._pin = pin
        self.clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, PendingApproval] = {}
        self._notices: list[float] = []

    def __repr__(self) -> str:
        return f"ApprovalInbox({len(self._entries)} entries)"

    # --- internal, under the lock ---

    def _expire(self, entry: PendingApproval, now: float) -> None:
        if entry.state == "pending" and now >= entry.expires_at:
            self._finish(entry, "expired")

    def _finish(self, entry: PendingApproval, state: str) -> None:
        entry.state = state
        entry.done.set()
        log.info("approval %s level %s -> %s", entry.id[:6], entry.level, state)

    def _prune(self, now: float) -> None:
        for entry in list(self._entries.values()):
            self._expire(entry, now)
            if now > entry.expires_at + PRUNE_AFTER_S:
                del self._entries[entry.id]

    # --- API ---

    def register(self, tool_name: str, level: int, summary: str, timeout_s: int) -> PendingApproval | None:
        """Create a pending approval and send the notice. None means: deny."""
        now = self.clock()
        lifetime = min(timeout_s, self.max_timeout_s) - EXPIRY_MARGIN_S
        if level not in (Level.WRITE, Level.DANGER) or lifetime <= 0:
            return None
        if level == Level.DANGER and not self._pin:
            log.warning("level-3 remote approval refused: PA_APPROVAL_PIN is not set")
            return None
        with self._lock:
            self._prune(now)
            if sum(1 for e in self._entries.values() if e.state == "pending") >= MAX_PENDING:
                log.warning("remote approval refused: too many pending")
                return None
            self._notices = [t for t in self._notices if now - t < NOTICE_WINDOW_S]
            if len(self._notices) >= NOTICE_LIMIT:
                log.warning("remote approval refused: notice rate limit reached")
                return None
            self._notices.append(now)
            entry = PendingApproval(
                id=secrets.token_urlsafe(24),
                tool_name=tool_name[:200],  # shown HTML-escaped; the broker already ASCII-escaped it
                level=int(level),
                summary=summary,
                expires_at=now + lifetime,
                csrf=secrets.token_urlsafe(24),
            )
            self._entries[entry.id] = entry
        # The post may take seconds: never hold the lock over the network.
        texts = _texts(self.language)
        notice = texts["notice"].format(
            level=entry.level,
            tool=discord_safe(tool_name, 60),
            link=f"{self.public_base}/approve/{entry.id}",
            minutes=max(1, round(lifetime / 60)),
        )
        if not self.notifier.post(notice):
            with self._lock:
                self._entries.pop(entry.id, None)
                self._finish(entry, "denied")
            return None
        log.info("approval %s level %s pending, notice sent", entry.id[:6], entry.level)
        return entry

    def get(self, approval_id: str) -> PendingApproval | None:
        """The entry while it can still be decided, else None."""
        with self._lock:
            entry = self._entries.get(approval_id)
            if entry is None:
                return None
            self._expire(entry, self.clock())
            return entry if entry.state == "pending" else None

    def resolve(self, approval_id: str, csrf: str, decision: str, confirm: str = "", pin: str = "") -> str:
        """ok | invalid | bad_csrf | bad_request | need_confirm | bad_pin | locked."""
        with self._lock:
            entry = self._entries.get(approval_id)
            if entry is None:
                return "invalid"
            self._expire(entry, self.clock())
            if entry.state != "pending":
                return "invalid"
            if not hmac.compare_digest(csrf.encode("utf-8"), entry.csrf.encode("utf-8")):
                return "bad_csrf"
            if decision == "deny":
                self._finish(entry, "denied")
                return "ok"
            if decision != "allow":
                return "bad_request"
            if entry.level >= Level.DANGER:
                if confirm.strip().lower() != CONFIRM_WORD:
                    return "need_confirm"
                expected = self._pin or ""
                if not expected or not hmac.compare_digest(pin.strip().encode("utf-8"), expected.encode("utf-8")):
                    entry.pin_attempts += 1
                    log.warning("approval %s: wrong PIN", entry.id[:6])
                    if entry.pin_attempts >= MAX_PIN_ATTEMPTS:
                        self._finish(entry, "denied")
                        return "locked"
                    return "bad_pin"
            self._finish(entry, "allowed")
            return "ok"

    def cancel(self, approval_id: str) -> bool:
        """The agent stopped waiting: a later click must not show "approved"."""
        with self._lock:
            entry = self._entries.get(approval_id)
            if entry is None:
                return False
            if entry.state == "pending":
                self._finish(entry, "expired")
            return True

    def wait(self, approval_id: str, timeout_s: float) -> str | None:
        """The state after at most timeout_s (capped at 30 s); None for an unknown id."""
        end = time.monotonic() + max(0.0, min(timeout_s, MAX_WAIT_S))
        while True:
            with self._lock:
                entry = self._entries.get(approval_id)
                if entry is None:
                    return None
                self._expire(entry, self.clock())
                if entry.state != "pending":
                    return entry.state
            remaining = end - time.monotonic()
            if remaining <= 0:
                return "pending"
            entry.done.wait(min(remaining, 1.0))

    def pin_attempts_left(self, entry: PendingApproval) -> int:
        return max(0, MAX_PIN_ATTEMPTS - entry.pin_attempts)


# --- pages ---------------------------------------------------------------------

_STYLE = (
    "body{font-family:system-ui,sans-serif;max-width:36rem;margin:1.5rem auto;padding:0 1rem;"
    "line-height:1.4;color:#1a1a1a;background:#fafafa}"
    "@media (prefers-color-scheme:dark){body{color:#eee;background:#161616}}"
    "pre{white-space:pre-wrap;word-break:break-all;padding:.6rem;border:1px solid #8886}"
    "input,button{font-size:1.05rem;padding:.5rem;margin:.25rem 0}"
    "button{min-width:8rem;margin-right:.5rem}.err{color:#c62828;font-weight:600}"
)


def _page(title: str, body: str, language: str) -> str:
    return (
        f'<!doctype html><html lang="{html.escape(language)}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body>{body}</body></html>"
    )


def render_page(p: PendingApproval, language: str = "en", error: str | None = None) -> str:
    """The approval form. No JavaScript; every dynamic value is escaped."""
    t = _texts(language)
    level = Level(p.level)
    expires = time.strftime("%H:%M:%S", time.localtime(p.expires_at))
    parts = [
        f"<h1>{html.escape(t['title'])}</h1>",
        f"<p><strong>{html.escape(p.tool_name)}</strong> &middot; "
        f"{html.escape(t['level'].format(level=int(level), label=LEVEL_LABELS[level]))}</p>",
        f"<pre>{html.escape(p.summary)}</pre>",
        f"<p>{html.escape(t['expires'].format(time=expires))}</p>",
    ]
    if error:
        parts.append(f'<p class="err">{html.escape(error)}</p>')
    form = [
        '<form method="post">',
        f'<input type="hidden" name="csrf" value="{html.escape(p.csrf, quote=True)}">',
    ]
    if level >= Level.DANGER:
        form += [
            f'<p><label>{html.escape(t["confirm"].format(word=CONFIRM_WORD))}<br>'
            '<input name="confirm" autocomplete="off" autocapitalize="none" spellcheck="false"></label></p>',
            f'<p><label>{html.escape(t["pin"])}<br>'
            '<input name="pin" type="password" inputmode="numeric" autocomplete="off"></label></p>',
        ]
    form += [
        f'<p><button name="decision" value="allow">{html.escape(t["allow"])}</button>'
        f'<button name="decision" value="deny">{html.escape(t["deny"])}</button></p>',
        "</form>",
    ]
    return _page(t["title"], "".join(parts + form), language)


def render_message(text: str, language: str = "en") -> str:
    """A page for a final, invalid or rejected state."""
    return _page(_texts(language)["title"], f"<p>{html.escape(text)}</p>", language)


# --- the channel in the chat process -------------------------------------------


class RemoteChannel:
    """Approval channel that asks through the running `serve` process."""

    def __init__(
        self,
        base_url: str,
        api_token: str,
        lock: threading.Lock,
        write: Callable[[str], None] = print,
        opener: Callable | None = None,
        poll_s: int = 2,  # short polls: a stop is noticed (and cancelled) within the broker's close join
        stop: Callable[[], bool] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = api_token
        self.lock = lock
        self.write = write
        self._opener = opener or local_opener()
        self.poll_s = poll_s
        self._stop = stop or (lambda: False)

    def __repr__(self) -> str:
        return f"RemoteChannel({self.base_url!r})"

    def _note(self, line: str) -> None:
        if self.lock.acquire(timeout=5):
            try:
                self.write(line)
            finally:
                self.lock.release()

    def _request(self, method: str, path: str, payload: dict | None, timeout: float) -> tuple[int, dict]:
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        request.add_unredirected_header("Authorization", f"Bearer {self._token}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self._opener(request, timeout=timeout) as response:
                status, body = response.status, response.read(4096)
        except urllib.error.HTTPError as exc:
            status, body = exc.code, b""
            _close_quietly(exc)
        reply = json.loads(body) if body else {}
        return status, reply if isinstance(reply, dict) else {}

    def ask(self, request: ApprovalRequest, deadline: float) -> bool:
        if request.full is not None:
            self._note(f"[approval for {request.tool_name} is too large to review remotely: denied]")
            return False
        remaining = int(deadline - time.monotonic())
        if remaining <= EXPIRY_MARGIN_S:
            return False
        approval_id: str | None = None
        state = "pending"
        try:
            status, reply = self._request(
                "POST",
                "/internal/approvals",
                {"tool_name": request.tool_name, "level": int(request.level),
                 "summary": (f"({request.context}: {CONTEXT_HINT})\n"
                             if request.context else "") + request.summary,
                 "timeout_s": remaining},
                # The server posts the notice before it answers. Wait as long as the entry
                # can live: if this call gives up, the entry it may have created has expired.
                timeout=remaining,
            )
            if status == 503:
                self._note("[remote approvals are not configured on the server: denied]")
                return False
            candidate = reply.get("id")
            if status != 200 or not isinstance(candidate, str) or not ID_RE.fullmatch(candidate):
                self._note(f"[remote approval not available (HTTP {status}): denied]")
                return False
            approval_id = candidate
            self._note(f"[approval pending remotely: {request.tool_name} (level {int(request.level)}), link sent]")
            while state == "pending":
                if self._stop():
                    break
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                wait = max(0, min(self.poll_s, int(left)))
                status, reply = self._request(
                    "GET", f"/internal/approvals/{approval_id}?wait={wait}", None, timeout=wait + 10
                )
                new_state = reply.get("state")
                if status != 200 or new_state not in STATES:
                    state = "unknown"
                    break
                state = new_state
                if state == "pending" and wait == 0:
                    time.sleep(min(0.2, max(left, 0)))
        except Exception as exc:  # noqa: BLE001 - every failure is a deny
            self._note(f"[remote approval failed ({type(exc).__name__}): denied]")
            state = "error"
        finally:
            # Cancel on every path that did not end in a final state (deadline, stop, poll
            # error, bad reply), so a later click can never show "approved" for a denial.
            if approval_id is not None and state not in FINAL_STATES:
                try:
                    self._request("POST", f"/internal/approvals/{approval_id}/cancel", {}, timeout=3)
                except Exception:  # noqa: BLE001 - best effort; the entry expires anyway
                    pass
        if state == "allowed":
            self._note("[approved remotely]")
            return True
        if state in ("pending", "expired"):
            self._note("[remote approval timed out: denied]")
        elif state == "denied":
            self._note("[denied remotely]")
        return False
