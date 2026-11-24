"""The route table of the web listener: one row per method and path, nothing else exists.

Every row names who may call it:
  public   no sign-in (sign-in itself, session state, health)
  session  a signed-in session (sign-in arrives with L1-3; until then the answer is 401)
  confirm  a session plus a fresh second factor (5 min)
  own      the route's existing check: the one-time approval link, the Twilio signature,
           the device token of /chat
  static   the built app shell, holds no data

A test pins this table: a route that is not listed here is never served, and the
internal routes of the other listener (/internal/..., /escalate) are not listed at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ACCESS = ("public", "session", "confirm", "own", "static")
_SEGMENT = re.compile(r"[A-Za-z0-9_-]{1,64}")


@dataclass(frozen=True)
class Route:
    method: str
    path: str      # literal segments, "<name>" for one id segment, a trailing "*" for the rest
    access: str
    name: str      # handler key in the listener

    def match(self, path: str) -> dict[str, str] | None:
        """The path parameters if `path` fits this pattern, else None."""
        if self.path.endswith("*"):
            prefix = self.path[:-1]
            return {"rest": path[len(prefix):]} if path.startswith(prefix) else None
        want, have = self.path.split("/"), path.split("/")
        if len(want) != len(have):
            return None
        params = {}
        for w, h in zip(want, have):
            if w.startswith("<") and w.endswith(">"):
                if not _SEGMENT.fullmatch(h):
                    return None
                params[w[1:-1]] = h
            elif w != h:
                return None
        return params


ROUTES: tuple[Route, ...] = (
    Route("GET", "/healthz", "public", "health"),
    Route("GET", "/api/session", "public", "session_state"),
    Route("POST", "/api/login/password", "public", "login_password"),
    Route("POST", "/api/login/totp", "public", "login_totp"),
    Route("POST", "/api/login/passkey/begin", "public", "login_passkey_begin"),
    Route("POST", "/api/login/passkey/finish", "public", "login_passkey_finish"),
    Route("POST", "/api/recovery", "public", "recovery"),
    Route("POST", "/api/logout", "session", "logout"),
    Route("POST", "/api/logout-all", "session", "logout_all"),
    Route("GET", "/api/today", "session", "today"),
    Route("GET", "/api/approvals", "session", "approvals"),
    Route("POST", "/api/approvals/<id>", "session", "approval_decide"),  # level 3 asks for "confirm" itself
    Route("GET", "/api/system", "session", "system"),
    Route("GET", "/api/settings", "session", "settings_get"),
    Route("PUT", "/api/settings", "session", "settings_put"),  # marked groups ask for "confirm" themselves
    Route("PUT", "/api/secrets/<name>", "confirm", "secret_put"),
    Route("DELETE", "/api/secrets/<name>", "confirm", "secret_delete"),
    Route("GET", "/api/mail/accounts", "session", "mail_accounts"),
    Route("PUT", "/api/mail/accounts", "confirm", "mail_accounts_put"),
    Route("DELETE", "/api/mail/accounts", "confirm", "mail_accounts_delete"),
    Route("POST", "/api/mail/test", "session", "mail_test"),
    Route("POST", "/api/mail/test-draft", "confirm", "mail_test_draft"),
    Route("POST", "/api/telegram/pair", "confirm", "telegram_pair"),
    Route("POST", "/api/telegram/unpair", "confirm", "telegram_unpair"),
    Route("POST", "/api/push/subscribe", "session", "push_subscribe"),
    Route("DELETE", "/api/push/subscribe", "session", "push_unsubscribe"),
    Route("GET", "/api/passkeys", "session", "passkeys"),
    Route("POST", "/api/passkeys", "confirm", "passkey_add"),
    Route("DELETE", "/api/passkeys", "confirm", "passkey_delete"),
    Route("GET", "/api/totp", "session", "totp"),
    Route("POST", "/api/totp", "confirm", "totp_add"),
    Route("DELETE", "/api/totp", "confirm", "totp_delete"),
    Route("GET", "/api/events", "session", "events"),
    Route("GET", "/app/*", "static", "app"),
    Route("GET", "/approve/<id>", "own", "approve_page"),
    Route("POST", "/approve/<id>", "own", "approve_post"),
    Route("POST", "/twilio/voice", "own", "twilio"),
    Route("POST", "/twilio/gather", "own", "twilio"),
    Route("POST", "/chat", "own", "chat_post"),
    Route("GET", "/chat/<id>", "own", "chat_result"),
)

assert all(r.access in ACCESS for r in ROUTES)


def resolve(method: str, path: str) -> tuple[Route | None, dict[str, str], tuple[str, ...]]:
    """(route, params, allowed methods). No route and no methods: 404; methods but no route: 405."""
    allowed = []
    for route in ROUTES:
        params = route.match(path)
        if params is None:
            continue
        if route.method == method:
            return route, params, ()
        allowed.append(route.method)
    return None, {}, tuple(sorted(set(allowed)))
