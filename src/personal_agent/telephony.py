"""Phone providers: a fake one for development and Twilio for real calls.

Twilio is spoken to with the standard library only (REST call + TwiML), which
keeps the dependency list empty and the code easy to audit.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import urllib.parse
import urllib.request
from typing import Protocol
from xml.sax.saxutils import escape, quoteattr

from .config import TwilioConfig
from .conversation import Turn

log = logging.getLogger(__name__)

TWILIO_LANGUAGE = {"en": "en-US", "de": "de-DE"}


def mask_number(number: str | None) -> str:
    """+447700900123 -> +44******0123 (for logs)."""
    if not number:
        return "<none>"
    return number[:3] + "*" * max(len(number) - 7, 0) + number[-4:]


class CallProvider(Protocol):
    def place_call(self, to: str, call_id: str) -> str:
        """Start an outbound call. Returns a provider reference for logs."""


class FakeProvider:
    """Places no call. Logs what would happen; use `personal-agent simulate` to talk."""

    def __init__(self) -> None:
        self.placed: list[tuple[str, str]] = []

    def place_call(self, to: str, call_id: str) -> str:
        self.placed.append((to, call_id))
        log.info("fake provider: would call %s (call %s)", mask_number(to), call_id[:6])
        return "fake"


class TwilioProvider:
    API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json"

    def __init__(self, config: TwilioConfig, public_url: str, timeout_s: int = 15) -> None:
        self.config = config
        self.public_url = public_url
        self.timeout_s = timeout_s

    def place_call(self, to: str, call_id: str) -> str:
        query = urllib.parse.urlencode({"cid": call_id})
        body = urllib.parse.urlencode(
            {
                "To": to,
                "From": self.config.from_number,
                "Url": f"{self.public_url}/twilio/voice?{query}",
                "Method": "POST",
                "Timeout": "30",
            }
        ).encode()
        request = urllib.request.Request(
            self.API.format(sid=urllib.parse.quote(self.config.account_sid)), data=body, method="POST"
        )
        credentials = f"{self.config.account_sid}:{self.config.auth_token}".encode()
        request.add_header("Authorization", "Basic " + base64.b64encode(credentials).decode())
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:  # noqa: S310 (fixed https URL)
            data = json.loads(response.read().decode())
        log.info("twilio: calling %s (call %s)", mask_number(to), call_id[:6])
        return str(data.get("sid", "unknown"))


def twilio_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    """Signature Twilio puts in X-Twilio-Signature for a form POST."""
    payload = url + "".join(key + params[key] for key in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def valid_twilio_signature(auth_token: str, url: str, params: dict[str, str], signature: str | None) -> bool:
    if not signature:
        return False
    expected = twilio_signature(auth_token, url, params)
    return hmac.compare_digest(expected.encode(), signature.encode())


def render_twiml(turn: Turn, action_url: str) -> str:
    """Turn a conversation step into TwiML."""
    language = TWILIO_LANGUAGE.get(turn.language, "en-US")
    lang_attr = quoteattr(language)
    says = "".join(f"<Say language={lang_attr}>{escape(text)}</Say>" for text in turn.say)
    if turn.expect is None:
        return f'<?xml version="1.0" encoding="UTF-8"?><Response>{says}<Hangup/></Response>'
    if turn.expect == "pin":
        gather_attrs = 'input="dtmf" finishOnKey="#" timeout="10"'
    else:
        gather_attrs = f'input="dtmf speech" numDigits="1" timeout="6" speechTimeout="auto" language={lang_attr}'
    action = quoteattr(action_url)
    return (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        f'<Gather {gather_attrs} action={action} method="POST">{says}</Gather>'
        f'<Redirect method="POST">{escape(action_url)}</Redirect>'
        "</Response>"
    )
