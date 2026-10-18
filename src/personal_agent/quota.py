"""Quota guard: never start a turn when the subscription quota is nearly used up.

Claude Code reports the subscription windows in its stream-json output as a
`rate_limit_event`. The guard keeps the last report (reduced to a few
whitelisted fields) and refuses a turn before any model is called when

- the reported status is anything other than "allowed" or "allowed_warning"
  ("allowed_warning" only means the limit is approaching; the threshold below
  decides when to stop),
- paid overage is in use, or
- the five-hour or seven-day window is at or above the stop threshold.

A condition only blocks until its reset time. When the reset time is unknown it
blocks for PROBE_AFTER_S, after which one probe turn may refresh the data;
otherwise stale data would block forever, because only a turn brings new data.
Paid overage is the exception: a probe turn would itself be billed, so it blocks
until the latest known reset, or, with none known, until `quota reset`.

Paid fallback is excluded twice: the bridge never passes API keys, and a session
whose init event reports an API key source is stopped (see `block_paid`).
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

PROBE_AFTER_S = 15 * 60
WINDOWS = ("five_hour", "seven_day")
ALLOWED_STATUSES = ("allowed", "allowed_warning")  # documented values: allowed, allowed_warning, rejected
WINDOW_LABELS = {"five_hour": "five-hour window", "seven_day": "seven-day window"}
SUBSCRIPTION_KEY_SOURCE = "none"  # init event `apiKeySource` when no API key is in use
RESET_COMMAND = "quota reset"     # chat command that clears the saved quota data


@dataclass(frozen=True)
class QuotaCheck:
    ok: bool
    reason: str
    fresh: bool = True  # False: no data, or older than check(require_fresh_s=...) allows


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def parse_reset(value: object) -> float | None:
    """Reset time as epoch seconds. Accepts seconds, milliseconds and ISO 8601."""
    if isinstance(value, str):
        text = value.strip()
        try:
            return parse_reset(float(text))
        except ValueError:
            pass
        try:
            parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text)
        except ValueError:
            return None
        return parsed.timestamp()
    number = _number(value)
    if number is None or number <= 0:
        return None
    return number / 1000 if number > 1e12 else number


def summarize(info: dict | None) -> dict:
    """Only the fields the guard needs; everything else in the event is dropped."""
    if not isinstance(info, dict):
        return {}
    status = info.get("status")
    windows_in = info.get("unifiedWindows")
    windows_in = windows_in if isinstance(windows_in, dict) else {}
    windows = {}
    for name in WINDOWS:
        window = windows_in.get(name)
        if isinstance(window, dict):
            windows[name] = {
                "utilization": _number(window.get("utilization")),
                "resetsAt": parse_reset(window.get("resetsAt")),
            }
        elif info.get("rateLimitType") == name and _number(info.get("utilization")) is not None:
            # Older reports only describe the currently limiting window at the top level.
            windows[name] = {
                "utilization": _number(info.get("utilization")),
                "resetsAt": parse_reset(info.get("resetsAt")),
            }
    return {
        "status": status[:40] if isinstance(status, str) else None,
        "resetsAt": parse_reset(info.get("resetsAt")),
        "isUsingOverage": info.get("isUsingOverage") is True or info.get("overageInUse") is True,
        "windows": windows,
    }


def is_overage(info: object) -> bool:
    return isinstance(info, dict) and (info.get("isUsingOverage") is True or info.get("overageInUse") is True)


def _clock_text(ts: float, now: float) -> str:
    fmt = "%H:%M" if ts - now < 20 * 3600 else "%a %d %b %H:%M"
    return time.strftime(fmt, time.localtime(ts))


def _percent(value: float) -> str:
    return f"{value * 100:.0f} %"


class QuotaGuard:
    """Shared by every session of one chat (they draw on the same subscription). Thread-safe."""

    def __init__(self, stop_at: float, state_file: Path | None, clock: Callable[[], float] = time.time) -> None:
        self.stop_at = stop_at
        self.state_file = state_file
        self.clock = clock
        self._lock = threading.Lock()
        self._paid_reason: str | None = None
        self._data = self._load()

    # --- state ----------------------------------------------------------------

    def _load(self) -> dict:
        if self.state_file is None:
            return {}
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        observed_at = _number(raw.get("observed_at"))
        if observed_at is None:
            return {}
        # Re-validate the saved summary: the file is data, not trusted state.
        info = {
            "status": raw.get("status"),
            "resetsAt": raw.get("resetsAt"),
            "isUsingOverage": raw.get("isUsingOverage"),
            "unifiedWindows": raw.get("windows"),
        }
        return {"observed_at": observed_at, **summarize(info)}

    def _save(self) -> None:
        if self.state_file is None:
            return
        # A unique temp name: the chat and the serve process (brief polish) may save at
        # the same time, and a shared fixed ".tmp" name would let one clobber the other.
        tmp: str | None = None
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.state_file.parent, prefix=".quota-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(self._data))
            os.replace(tmp, self.state_file)
            tmp = None
        except OSError as exc:
            log.warning("could not save quota state: %s", type(exc).__name__)
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def observe(self, info: dict | None) -> None:
        """Record one `rate_limit_info` object from a rate_limit_event."""
        summary = summarize(info)
        if not summary:
            return
        with self._lock:
            self._data = {"observed_at": self.clock(), **summary}
            self._save()

    def reset(self) -> None:
        """Forget the saved quota data (memory and file). A paid-usage block stays."""
        with self._lock:
            self._data = {}
            if self.state_file is not None:
                try:
                    self.state_file.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    log.warning("could not remove quota state: %s", type(exc).__name__)

    def block_paid(self, reason: str) -> None:
        """Refuse every further turn of this process (e.g. an API key is in use)."""
        with self._lock:
            self._paid_reason = reason

    # --- decisions --------------------------------------------------------------

    def _conditions(self, data: dict, now: float, stop_at: float) -> list[str]:
        """Reasons that block right now, with stop_at as the window threshold."""
        probe_until = data["observed_at"] + PROBE_AFTER_S
        found = []

        def blocking(reset: float | None) -> float | None:
            until = reset if reset is not None else probe_until
            return until if until > now else None

        def when(reset: float | None, until: float) -> str:
            return f"resets {_clock_text(until, now)}" if reset is not None else f"retry after {_clock_text(until, now)}"

        top_reset = data.get("resetsAt")
        if data.get("isUsingOverage"):
            # No probe turn here: the probe itself would be billed. Block until the latest
            # known reset, or until the person clears it with `quota reset`.
            resets = [top_reset] + [w.get("resetsAt") for w in data.get("windows", {}).values()]
            known = [r for r in resets if r is not None]
            if not known:
                found.append(f"paid overage is in use (switch it off, then type `{RESET_COMMAND}`)")
            elif max(known) > now:
                found.append(f"paid overage is in use (resets {_clock_text(max(known), now)})")
        status = data.get("status")
        if status not in ALLOWED_STATUSES:
            until = blocking(top_reset)
            if until is not None:
                shown = status if status else "unknown"
                found.append(f"quota status is '{shown}' ({when(top_reset, until)})")
        for name in WINDOWS:
            window = data.get("windows", {}).get(name)
            if not window or window.get("utilization") is None or window["utilization"] < stop_at:
                continue
            until = blocking(window.get("resetsAt"))
            if until is not None:
                found.append(
                    f"{WINDOW_LABELS[name]} at {_percent(window['utilization'])}, stop at "
                    f"{_percent(stop_at)} ({when(window.get('resetsAt'), until)})"
                )
        return found

    def check(self, stop_at: float | None = None, require_fresh_s: float | None = None) -> QuotaCheck:
        """Whether a turn may start. stop_at None means self.stop_at.

        Background helpers pass a lower stop_at, so they leave headroom for the chat.
        With require_fresh_s, `fresh` is False when there is no data or it is older;
        the caller then runs at most one probe turn at a time.
        """
        threshold = self.stop_at if stop_at is None else stop_at
        with self._lock:
            now = self.clock()
            fresh = True
            if require_fresh_s is not None:
                fresh = bool(self._data) and now - self._data["observed_at"] <= require_fresh_s
            if self._paid_reason:
                return QuotaCheck(False, self._paid_reason, fresh)
            if not self._data:
                return QuotaCheck(True, "no quota data yet", fresh)
            found = self._conditions(self._data, now, threshold)
        if found:
            return QuotaCheck(False, "; ".join(found), fresh)
        return QuotaCheck(True, "within quota", fresh)

    def describe(self) -> str:
        """One line for the `quota` command."""
        with self._lock:
            data = dict(self._data)
        now = self.clock()
        parts = []
        if data:
            for name in WINDOWS:
                window = data.get("windows", {}).get(name)
                if not window or window.get("utilization") is None:
                    continue
                text = f"{WINDOW_LABELS[name]} {_percent(window['utilization'])}"
                if window.get("resetsAt") is not None:
                    text += f" (resets {_clock_text(window['resetsAt'], now)})"
                parts.append(text)
            parts.append(f"status {data.get('status') or 'unknown'}")
            if data.get("isUsingOverage"):
                parts.append("paid overage in use")
        else:
            parts.append("no quota data yet")
        parts.append(f"stop at {_percent(self.stop_at)}")
        check = self.check()
        if not check.ok:
            parts.append(f"blocked: {check.reason}")
        return " · ".join(parts)
