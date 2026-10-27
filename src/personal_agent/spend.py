"""Spend guard: bound the cost of an installation that pays per token (access mode "api").

In access mode "subscription" the QuotaGuard watches the plan's usage windows. With an
API key there are no such windows: every token is billed. This guard takes their place
and offers the same interface (check, observe, block_paid, reset, describe) plus
record_cost, which guarded_send calls after every turn.

Claude Code reports `total_cost_usd` in each `result` event. It is cumulative for the
running process (spike 2026-10-27: two turns in one `-p` process reported 0.215 and
0.233), so the guard books the difference to the last value it saw for that session; a
smaller value means a new process, and the whole value is new. The ledger holds the
cost per calendar day and month (local time) in state_dir/spend.json, written
atomically under an flock, because chat, serve, briefs and helpers share it.

A turn is refused before any model is called once the day or month cap is reached. The
cap can be overrun by at most one turn: the cost is only known when the turn ends.
Workspace spend limits in the Claude Console stay the hard outer bound.
"""

from __future__ import annotations

import fcntl
import logging
import math
import os
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .quota import QuotaCheck, QuotaGuard
from .statefiles import read_private_json, write_private_json

if TYPE_CHECKING:
    from .config import Config

log = logging.getLogger(__name__)

LEDGER_FILE = "spend.json"
LOCK_FILE = "spend.lock"
LEDGER_VERSION = 1
LEDGER_CAP = 4096
WARN_PERCENTS = (50, 80, 100)
SESSION_ATTR = "_pa_cost_seen"  # last cumulative cost seen on a session object


def _amount(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else None


class SpendGuard:
    def __init__(
        self,
        cap_day_usd: float,
        cap_month_usd: float,
        state_dir: Path,
        clock: Callable[[], float] = time.time,
        on_threshold: Callable[[int, dict], None] | None = None,
    ) -> None:
        self.cap_day = cap_day_usd
        self.cap_month = cap_month_usd
        self.state_dir = Path(state_dir)
        self.clock = clock
        self.on_threshold = on_threshold
        self._lock = threading.Lock()
        self._paid_reason: str | None = None

    # --- ledger ---------------------------------------------------------------------------

    def _periods(self) -> tuple[str, str]:
        now = time.localtime(self.clock())
        return time.strftime("%Y-%m-%d", now), time.strftime("%Y-%m", now)

    def _load(self) -> dict:
        day, month = self._periods()
        data = read_private_json(self.state_dir, LEDGER_FILE, LEDGER_CAP, LEDGER_VERSION) or {}
        ledger = {"version": LEDGER_VERSION, "day": day, "day_usd": 0.0, "month": month, "month_usd": 0.0, "warned": []}
        if data.get("month") == month:
            ledger["month_usd"] = _amount(data.get("month_usd")) or 0.0
            warned = data.get("warned")
            if isinstance(warned, list):
                ledger["warned"] = [p for p in warned if p in WARN_PERCENTS]
            if data.get("day") == day:
                ledger["day_usd"] = _amount(data.get("day_usd")) or 0.0
        return ledger

    def record_cost(self, session: object, total_usd: object) -> None:
        """Book the part of a session's cumulative cost that is new since the last turn."""
        total = _amount(total_usd)
        if total is None:
            return
        seen = _amount(getattr(session, SESSION_ATTR, None)) or 0.0
        delta = total if total < seen else total - seen
        try:
            setattr(session, SESSION_ATTR, total)
        except AttributeError:
            pass
        if delta <= 0:
            return
        warnings: list[tuple[int, dict]] = []
        with self._lock:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.state_dir / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                ledger = self._load()
                ledger["day_usd"] += delta
                ledger["month_usd"] += delta
                for percent in WARN_PERCENTS:
                    if percent not in ledger["warned"] and ledger["month_usd"] >= self.cap_month * percent / 100:
                        ledger["warned"].append(percent)
                        warnings.append((percent, dict(ledger)))
                write_private_json(self.state_dir, LEDGER_FILE, ledger)
            finally:
                os.close(fd)  # also releases the flock
        for percent, snapshot in warnings:
            if self.on_threshold is not None:
                try:
                    self.on_threshold(percent, snapshot)
                except Exception:  # a failing notice must never break the turn
                    log.warning("spend notice failed")

    # --- QuotaGuard interface -------------------------------------------------------------

    def check(self, stop_at: float | None = None, require_fresh_s: float | None = None) -> QuotaCheck:
        """stop_at and require_fresh_s exist for the QuotaGuard interface and are ignored."""
        if self._paid_reason:
            return QuotaCheck(False, self._paid_reason)
        ledger = self._load()
        if ledger["day_usd"] >= self.cap_day:
            return QuotaCheck(False, f"daily spend cap reached ({self.describe_ledger(ledger)})")
        if ledger["month_usd"] >= self.cap_month:
            return QuotaCheck(False, f"monthly spend cap reached ({self.describe_ledger(ledger)})")
        return QuotaCheck(True, self.describe_ledger(ledger))

    def observe(self, info: dict | None) -> None:
        """Usage-window reports do not apply to API keys."""

    def block_paid(self, reason: str) -> None:
        self._paid_reason = reason

    def reset(self) -> None:
        """The spend ledger is never cleared from chat; that would lift the caps."""

    def describe_ledger(self, ledger: dict) -> str:
        return (
            f"spend today ${ledger['day_usd']:.2f} of ${self.cap_day:.2f}, "
            f"this month ${ledger['month_usd']:.2f} of ${self.cap_month:.2f}"
        )

    def describe(self) -> str:
        return self.describe_ledger(self._load())


def make_guard(config: "Config") -> QuotaGuard | SpendGuard:
    """The usage guard for the installation's access mode (a fresh one sees other processes' usage)."""
    if config.access_mode == "api":
        def notify(percent: int, ledger: dict) -> None:
            from .notify import Notifier, spend_notice  # lazy: notify imports much of the package

            Notifier.from_config(config, run_thread=False).send_now(
                "alert",
                spend_notice(percent, ledger["day_usd"], config.spend_cap_day_usd,
                             ledger["month_usd"], config.spend_cap_month_usd, config.language),
                key=f"spend-{percent}")

        return SpendGuard(config.spend_cap_day_usd, config.spend_cap_month_usd, config.state_dir,
                          on_threshold=notify)
    return QuotaGuard(config.quota_stop, config.state_dir / "quota.json")
