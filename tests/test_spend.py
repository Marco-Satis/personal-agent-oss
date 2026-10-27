"""Spend guard for access mode "api": ledger, caps, thresholds, wiring into guarded_send."""

import json
import os
import stat
import time
from pathlib import Path

import pytest

from personal_agent.bridge import BridgeConfig, TurnResult
from personal_agent.config import Config, ConfigError
from personal_agent.quota import QuotaGuard
from personal_agent.router import guarded_send
from personal_agent.spend import LEDGER_FILE, SpendGuard, make_guard

KEY = "sk-ant-test-key-value"  # pragma: allowlist secret
DAY1 = time.mktime((2026, 10, 27, 12, 0, 0, 0, 0, -1))
DAY2 = time.mktime((2026, 10, 28, 12, 0, 0, 0, 0, -1))
NEXT_MONTH = time.mktime((2026, 11, 2, 12, 0, 0, 0, 0, -1))


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class Session:
    """Stand-in for ClaudeSession: replays a cumulative cost per turn."""

    def __init__(self, totals=(), events=()):
        self.totals = list(totals)
        self.events = events
        self.config = BridgeConfig(workdir=Path("/w"), config_dir=Path("/c"), api_key=KEY)

    def send(self, text, on_event=None):
        for event in self.events:
            on_event(event)
        return TurnResult("ok", "sid", False, "success", [], self.totals.pop(0), None)


def guard(tmp_path, clock=None, day=3.0, month=40.0, on_threshold=None):
    return SpendGuard(day, month, tmp_path, clock=clock or Clock(DAY1), on_threshold=on_threshold)


def test_books_the_difference_of_the_cumulative_cost(tmp_path):
    g = guard(tmp_path)
    s = Session()
    g.record_cost(s, 0.20)
    g.record_cost(s, 0.25)  # second turn of the same process: +0.05
    assert "spend today $0.25" in g.describe()


def test_a_smaller_total_means_a_new_process(tmp_path):
    g = guard(tmp_path)
    s = Session()
    g.record_cost(s, 0.30)
    g.record_cost(s, 0.10)  # restarted process: the whole value is new
    assert "spend today $0.40" in g.describe()


@pytest.mark.parametrize("value", [None, "1.0", float("nan"), float("inf"), -1.0, True])
def test_invalid_costs_are_ignored(tmp_path, value):
    g = guard(tmp_path)
    g.record_cost(Session(), value)
    assert "spend today $0.00" in g.describe() and not (tmp_path / LEDGER_FILE).exists()


def test_sessions_are_tracked_separately(tmp_path):
    g = guard(tmp_path)
    a, b = Session(), Session()
    g.record_cost(a, 0.10)
    g.record_cost(b, 0.10)
    g.record_cost(a, 0.15)
    assert "spend today $0.25" in g.describe()


def test_daily_cap_blocks_and_lifts_the_next_day(tmp_path):
    clock = Clock(DAY1)
    g = guard(tmp_path, clock=clock, day=1.0)
    g.record_cost(Session(), 1.0)
    check = g.check()
    assert not check.ok and "daily spend cap" in check.reason
    clock.now = DAY2
    assert g.check().ok
    assert "this month $1.00" in g.describe()


def test_monthly_cap_blocks_until_next_month(tmp_path):
    clock = Clock(DAY1)
    g = guard(tmp_path, clock=clock, day=5.0, month=5.0)
    g.record_cost(Session(), 5.0)
    clock.now = DAY2
    check = g.check()
    assert not check.ok and "monthly spend cap" in check.reason
    clock.now = NEXT_MONTH
    assert g.check().ok


def test_ledger_is_shared_between_guards_and_private(tmp_path):
    guard(tmp_path).record_cost(Session(), 0.5)
    other = guard(tmp_path)  # another process
    assert "spend today $0.50" in other.describe()
    mode = stat.S_IMODE(os.stat(tmp_path / LEDGER_FILE).st_mode)
    assert mode == 0o600


def test_corrupt_ledger_counts_from_zero(tmp_path):
    (tmp_path / LEDGER_FILE).write_text("{not json")
    os.chmod(tmp_path / LEDGER_FILE, 0o600)
    assert guard(tmp_path).check().ok


def test_thresholds_fire_once_per_month(tmp_path):
    seen = []
    clock = Clock(DAY1)
    g = guard(tmp_path, clock=clock, day=100.0, month=10.0, on_threshold=lambda p, ledger: seen.append(p))
    s = Session()
    g.record_cost(s, 4.0)
    g.record_cost(s, 6.0)   # 60 %: 50 fires
    g.record_cost(s, 9.0)   # 90 %: 80 fires
    g.record_cost(s, 9.5)   # nothing new
    g.record_cost(s, 10.0)  # 100 %
    assert seen == [50, 80, 100]
    clock.now = NEXT_MONTH
    g.record_cost(Session(), 6.0)
    assert seen == [50, 80, 100, 50]


def test_failing_threshold_notice_does_not_break_the_turn(tmp_path):
    def boom(percent, ledger):
        raise RuntimeError("no channel")
    g = guard(tmp_path, month=1.0, on_threshold=boom)
    g.record_cost(Session(), 2.0)
    assert not g.check().ok


def test_reset_never_lifts_the_cap(tmp_path):
    g = guard(tmp_path, day=1.0)
    g.record_cost(Session(), 1.0)
    g.reset()
    assert not g.check().ok


def test_guarded_send_books_each_turn(tmp_path):
    g = guard(tmp_path)
    s = Session(totals=[0.10, 0.25])
    guarded_send(s, "one", g)
    guarded_send(s, "two", g)
    assert "spend today $0.25" in g.describe()


def test_ledger_content_has_no_prompt_text(tmp_path):
    g = guard(tmp_path)
    guarded_send(Session(totals=[0.1]), "secret customer question", g)
    assert "secret customer question" not in (tmp_path / LEDGER_FILE).read_text()
    assert json.loads((tmp_path / LEDGER_FILE).read_text())["version"] == 1


# --- configuration and factory ------------------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PA_STATE_DIR", str(tmp_path / "state"))
    for name in ("PA_ACCESS_MODE", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
                 "PA_SPEND_CAP_DAY_USD", "PA_SPEND_CAP_MONTH_USD"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_factory_picks_the_guard_for_the_access_mode(env):
    assert isinstance(make_guard(Config.from_env()), QuotaGuard)
    env.setenv("PA_ACCESS_MODE", "api")
    env.setenv("ANTHROPIC_API_KEY", KEY)
    env.setenv("PA_SPEND_CAP_MONTH_USD", "20")
    env.setenv("PA_SPEND_CAP_DAY_USD", "2.5")
    g = make_guard(Config.from_env())
    assert isinstance(g, SpendGuard) and g.cap_month == 20.0 and g.cap_day == 2.5


@pytest.mark.parametrize("day,month", [("0", "40"), ("50", "40"), ("1", "0"), ("1", "20000")])
def test_invalid_caps_are_errors(env, day, month):
    env.setenv("PA_SPEND_CAP_DAY_USD", day)
    env.setenv("PA_SPEND_CAP_MONTH_USD", month)
    with pytest.raises(ConfigError, match="PA_SPEND_CAP"):
        Config.from_env()


def test_factory_guard_sends_threshold_notices(env, monkeypatch):
    sent = []

    class FakeNotifier:
        @classmethod
        def from_config(cls, config, **kw):
            return cls()

        def send_now(self, kind, build, key=None, exclude=()):
            sent.append((kind, build(None), key))
            return True

    import personal_agent.notify as notify
    monkeypatch.setattr(notify, "Notifier", FakeNotifier)
    env.setenv("PA_ACCESS_MODE", "api")
    env.setenv("ANTHROPIC_API_KEY", KEY)
    env.setenv("PA_SPEND_CAP_MONTH_USD", "2")
    env.setenv("PA_SPEND_CAP_DAY_USD", "2")
    g = make_guard(Config.from_env())
    g.record_cost(Session(), 1.2)
    assert sent and sent[0][0] == "alert" and "50 %" in sent[0][1] and sent[0][2] == "spend-50"
