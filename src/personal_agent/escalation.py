"""Decides whether an event is worth a phone call.

A phone call is the loudest channel there is, so the default answer is "no".
Only `critical` events can call, and even those are limited by de-duplication,
a minimum interval between calls, a daily cap and optional quiet hours.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .status import clean_text

SEVERITIES = ("info", "warning", "critical")


@dataclass(frozen=True)
class Event:
    key: str
    severity: str
    summary: str
    source: str = "unknown"

    @classmethod
    def from_dict(cls, data: dict) -> "Event":
        if not isinstance(data, dict):
            raise ValueError("event must be a JSON object")
        severity = data.get("severity", "info")
        if severity not in SEVERITIES:
            raise ValueError(f"severity must be one of {', '.join(SEVERITIES)}")
        key = clean_text(data.get("key") or "", 80)
        if not key:
            raise ValueError("key is required")
        return cls(
            key=key,
            severity=severity,
            summary=clean_text(data.get("summary") or key),
            source=clean_text(data.get("source") or "unknown", 40),
        )


@dataclass(frozen=True)
class Decision:
    call: bool
    reason: str


def in_quiet_hours(hour: int, quiet: tuple[int, int] | None) -> bool:
    if quiet is None:
        return False
    start, end = quiet
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


class EscalationPolicy:
    """Rate-limits calls. State is persisted so a restart does not reset limits."""

    def __init__(
        self,
        state_file: Path,
        max_calls_per_day: int,
        min_interval_min: int,
        quiet_hours: tuple[int, int] | None,
    ) -> None:
        self.state_file = state_file
        self.max_calls_per_day = max_calls_per_day
        self.min_interval_s = min_interval_min * 60
        self.quiet_hours = quiet_hours
        self._lock = threading.Lock()
        self._state = self._load()

    def _load(self) -> dict:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("calls", [])
                data.setdefault("acknowledged", {})
                return data
        except (OSError, ValueError):
            pass
        return {"calls": [], "acknowledged": {}}

    def _save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state), encoding="utf-8")
        tmp.replace(self.state_file)

    def decide(self, event: Event, now: float | None = None) -> Decision:
        now = now or time.time()
        with self._lock:
            calls = [c for c in self._state["calls"] if now - c["at"] < 24 * 3600]
            self._state["calls"] = calls

            if event.severity != "critical":
                return Decision(False, f"severity {event.severity} never calls")
            ack_at = self._state["acknowledged"].get(event.key)
            if ack_at is not None and now - ack_at < 24 * 3600:
                return Decision(False, "acknowledged within the last 24 h")
            if any(c["key"] == event.key and now - c["at"] < max(self.min_interval_s, 3600) for c in calls):
                return Decision(False, "same key called recently")
            if calls and now - calls[-1]["at"] < self.min_interval_s:
                return Decision(False, "minimum interval between calls not reached")
            if len(calls) >= self.max_calls_per_day:
                return Decision(False, "daily call limit reached")
            if in_quiet_hours(datetime.fromtimestamp(now).hour, self.quiet_hours):
                return Decision(False, "quiet hours")

            calls.append({"key": event.key, "at": now})
            self._save()
            return Decision(True, "critical event within limits")

    def acknowledge(self, key: str, now: float | None = None) -> None:
        with self._lock:
            self._state["acknowledged"][key] = now or time.time()
            self._save()
