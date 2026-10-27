"""Sorting mail without a model: categories, the payment guard and mailbox health.

Everything here is plain code over data the mail worker already cleaned. Nothing calls a
model, nothing touches the network, and nothing a sender writes can trigger an action:
a category only changes how a mail is listed, a payment warning only adds a line.

Categories come from the subject and the list/auto flags (the body is never fetched
for an overview). The order matters: a "Mahnung" is a deadline before it is a payment.

The payment guard looks at a mail body that the person opened (`mail show`). It finds
IBANs (ISO 13616 check digits), shows them masked, and compares them with the IBANs
seen before from the same sender address, stored only as salted hashes in
state_dir/payees.json. The first IBAN of a sender is remembered and flagged as new; any
other IBAN from that sender later is flagged as changed, every time, until the person
confirms it with `mail trust <id>`. A changed bank account is the classic invoice fraud.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .statefiles import read_private_json, write_private_json

CATEGORIES = ("deadline", "payment", "complaint", "request", "confidential", "automated", "other")
LABELS = {
    "en": {"deadline": "deadline/legal", "payment": "invoice/payment", "complaint": "complaint",
           "request": "request", "confidential": "confidential", "automated": "newsletter/automatic",
           "other": "other"},
    "de": {"deadline": "Frist/Recht", "payment": "Rechnung/Zahlung", "complaint": "Reklamation",
           "request": "Anfrage", "confidential": "Vertraulich", "automated": "Newsletter/automatisch",
           "other": "Sonstiges"},
}

# Word stems (German and English), matched at a word start, case-insensitive.
_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("deadline", ("frist", "mahnung", "mahnbescheid", "kündigung", "kuendigung", "widerspruch", "steuerbescheid",
                  "gericht", "anwalt", "rechtsanwalt", "abmahnung", "vollstreckung", "inkasso", "letzte erinnerung",
                  "zahlungserinnerung", "deadline", "final notice", "overdue", "legal notice", "court")),
    ("payment", ("rechnung", "zahlung", "überweisung", "ueberweisung", "lastschrift", "gutschrift", "beleg",
                 "quittung", "invoice", "payment", "receipt", "bankverbindung", "iban")),
    ("complaint", ("reklamation", "beschwerde", "mangel", "mängel", "defekt", "kaputt", "unzufrieden",
                   "rückgabe", "rueckgabe", "garantie", "gewährleistung", "complaint", "refund", "broken",
                   "not working", "storno")),
    ("confidential", ("passwort", "password", "kennwort", "zugangsdaten", "sicherheitscode", "verifizierung",
                      "verification code", "2fa", "einmalcode", "vertraulich", "confidential")),
    ("request", ("anfrage", "angebot", "kostenvoranschlag", "termin", "frage", "preis", "auftrag",
                 "bestellung", "reservierung", "request", "quote", "inquiry", "appointment", "booking", "order")),
)
_PATTERNS = tuple((cat, re.compile(r"(?<![\w])(?:" + "|".join(re.escape(w) for w in words) + r")", re.IGNORECASE))
                  for cat, words in _RULES)


def classify(subject: str, *, is_list: bool = False, is_auto: bool = False) -> str:
    """One category for a mail from its subject and flags."""
    text = subject or ""
    for category, pattern in _PATTERNS:
        if pattern.search(text):
            # Newsletters say "Angebot" all the time: a list mail is never a request.
            if category == "request" and (is_list or is_auto):
                return "automated"
            return category
    if is_list or is_auto:
        return "automated"
    return "other"


def label(category: str, language: str = "en") -> str:
    return LABELS.get(language, LABELS["en"]).get(category, category)


def category_counts(items: Iterable[tuple[str, bool, bool]]) -> list[tuple[str, int]]:
    """(category, count) in CATEGORIES order for (subject, is_list, is_auto) triples; zeros left out."""
    counts = dict.fromkeys(CATEGORIES, 0)
    for subject, is_list, is_auto in items:
        counts[classify(subject, is_list=is_list, is_auto=is_auto)] += 1
    return [(c, n) for c, n in counts.items() if n]


# --- IBANs ---------------------------------------------------------------------------------

_IBAN_CANDIDATE = re.compile(r"\b([A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30})\b")
_IBAN_LENGTHS = {"DE": 22, "AT": 20, "CH": 21, "NL": 18, "BE": 16, "FR": 27, "IT": 27, "ES": 24, "LU": 20,
                 "PL": 28, "CZ": 24, "DK": 18, "SE": 24, "NO": 15, "FI": 18, "IE": 22, "GB": 22, "PT": 25,
                 "LI": 21, "HU": 28, "SK": 24, "SI": 19, "HR": 21, "RO": 24, "BG": 22, "GR": 27, "LT": 20,
                 "LV": 21, "EE": 20, "MT": 31, "CY": 28, "IS": 26}


def _iban_ok(iban: str) -> bool:
    if _IBAN_LENGTHS.get(iban[:2], 0) not in (0, len(iban)) or not 15 <= len(iban) <= 34:
        return False
    rearranged = iban[4:] + iban[:4]
    digits = "".join(str(int(c, 36)) for c in rearranged)
    return int(digits) % 97 == 1


def find_ibans(text: str) -> list[str]:
    """Valid IBANs in text (normalized, no spaces), at most 10, in order of appearance."""
    found: list[str] = []
    for match in _IBAN_CANDIDATE.finditer((text or "").upper()):
        iban = match.group(1).replace(" ", "")
        # A greedy match can swallow the following word; shorten to the country length.
        expected = _IBAN_LENGTHS.get(iban[:2])
        if expected and len(iban) > expected:
            iban = iban[:expected]
        if _iban_ok(iban) and iban not in found:
            found.append(iban)
        if len(found) >= 10:
            break
    return found


def mask_iban(iban: str) -> str:
    """Country, check digits and the last four characters: DE89 **** 3000."""
    return f"{iban[:4]} **** {iban[-4:]}"


@dataclass(frozen=True)
class PayeeCheck:
    iban: str          # masked
    status: str        # "known", "new" (first IBAN seen from this sender), "changed", "no_sender"


class PayeeStore:
    """Salted hashes of the IBANs seen per sender address (state_dir/payees.json, 0600)."""

    FILE = "payees.json"
    VERSION = 1
    CAP = 512 * 1024
    MAX_SENDERS = 2000

    def __init__(self, state_dir: Path, clock=time.time) -> None:
        self.state_dir = Path(state_dir)
        self.clock = clock

    def _load(self) -> dict:
        data = read_private_json(self.state_dir, self.FILE, self.CAP, self.VERSION)
        if not data or not isinstance(data.get("salt"), str) or not isinstance(data.get("senders"), dict):
            return {"version": self.VERSION, "salt": secrets.token_hex(16), "senders": {}}
        return data

    def _save(self, data: dict) -> None:
        senders = data["senders"]
        if len(senders) > self.MAX_SENDERS:  # forget the oldest senders
            for key in sorted(senders, key=lambda k: senders[k].get("seen", 0))[:len(senders) - self.MAX_SENDERS]:
                del senders[key]
        self.state_dir.mkdir(parents=True, exist_ok=True)
        write_private_json(self.state_dir, self.FILE, data)

    @staticmethod
    def _digest(salt: str, value: str) -> str:
        return hmac.new(bytes.fromhex(salt), value.encode(), hashlib.sha256).hexdigest()[:32]

    def check(self, sender: str | None, ibans: list[str]) -> list[PayeeCheck]:
        """Classify each IBAN for this sender; the first IBAN of a new sender is remembered."""
        if not ibans:
            return []
        if not sender:
            return [PayeeCheck(mask_iban(i), "no_sender") for i in ibans]
        data = self._load()
        key = self._digest(data["salt"], sender.lower())
        entry = data["senders"].get(key)
        out: list[PayeeCheck] = []
        if entry is None:
            entry = {"ibans": [self._digest(data["salt"], ibans[0])], "seen": self.clock()}
            data["senders"][key] = entry
            out.append(PayeeCheck(mask_iban(ibans[0]), "new"))
            rest = ibans[1:]
        else:
            rest = ibans
        known = set(entry.get("ibans", []))
        for iban in rest:
            status = "known" if self._digest(data["salt"], iban) in known else "changed"
            out.append(PayeeCheck(mask_iban(iban), status))
        entry["seen"] = self.clock()
        self._save(data)
        return out

    def trust(self, sender: str, ibans: list[str]) -> int:
        """The person confirmed these IBANs for this sender (after checking by phone)."""
        if not sender or not ibans:
            return 0
        data = self._load()
        key = self._digest(data["salt"], sender.lower())
        entry = data["senders"].setdefault(key, {"ibans": [], "seen": self.clock()})
        added = 0
        for iban in ibans:
            digest = self._digest(data["salt"], iban)
            if digest not in entry["ibans"]:
                entry["ibans"].append(digest)
                added += 1
        entry["ibans"] = entry["ibans"][-10:]
        entry["seen"] = self.clock()
        self._save(data)
        return added


PAYEE_TEXTS = {
    "en": {"known": "known for this sender", "new": "NEW: first bank account from this sender, check before paying",
           "changed": "CHANGED: this sender used a different bank account before; call them before paying",
           "no_sender": "sender address unknown; check before paying"},
    "de": {"known": "bekannt für diesen Absender",
           "new": "NEU: erste Bankverbindung dieses Absenders, vor Zahlung prüfen",
           "changed": "GEÄNDERT: dieser Absender hatte bisher eine andere Bankverbindung; vor Zahlung anrufen",
           "no_sender": "Absenderadresse unbekannt; vor Zahlung prüfen"},
}


def payee_lines(checks: list[PayeeCheck], language: str = "en") -> list[str]:
    texts = PAYEE_TEXTS.get(language, PAYEE_TEXTS["en"])
    return [f"IBAN {c.iban}: {texts[c.status]}" for c in checks]


# --- mailbox health ------------------------------------------------------------------------

HEALTH_TEXTS = {
    "en": {"auth": "login failed (was the password or app password changed?)",
           "stale": "not checked for {hours} h", "quiet": "no new mail for {days} working days",
           "error": "unavailable ({code})"},
    "de": {"auth": "Anmeldung fehlgeschlagen (Passwort oder App-Passwort geändert?)",
           "stale": "seit {hours} h nicht geprüft", "quiet": "seit {days} Werktagen keine neue Mail",
           "error": "nicht erreichbar ({code})"},
}
STALE_AFTER_S = 6 * 3600
QUIET_WORKDAYS = 3


def _workdays_between(start: float, end: float) -> int:
    days = 0
    t = start
    while t < end and days < 30:
        t += 86400
        if time.localtime(t).tm_wday < 5 and t <= end:
            days += 1
    return days


def health_notes(view: "object", now: float, language: str = "en") -> list[str]:
    """Problems a person should know about, one line per account. Empty when all is well.

    A dead login looks like a quiet mailbox; these notes keep the brief from looking calm
    when it is really blind.
    """
    texts = HEALTH_TEXTS.get(language, HEALTH_TEXTS["en"])
    notes: list[str] = []
    for account in getattr(view, "accounts", ()):
        name = account.name
        if account.error == "auth":
            notes.append(f"{name}: {texts['auth']}")
            continue
        if account.error:
            notes.append(f"{name}: {texts['error'].format(code=account.error)}")
            continue
        checked = account.checked_at
        if checked is None or now - checked > STALE_AFTER_S:
            hours = int((now - checked) // 3600) if checked is not None else "?"
            notes.append(f"{name}: {texts['stale'].format(hours=hours)}")
            continue
        dates = [m.date for f in account.folders for m in f.window if m.date is not None]
        newest = max(dates) if dates else None
        if newest is not None and _workdays_between(newest, now) >= QUIET_WORKDAYS:
            notes.append(f"{name}: {texts['quiet'].format(days=_workdays_between(newest, now))}")
    return notes


__all__ = [
    "CATEGORIES", "PayeeCheck", "PayeeStore", "category_counts", "classify", "find_ibans", "health_notes",
    "label", "mask_iban", "payee_lines",
]
