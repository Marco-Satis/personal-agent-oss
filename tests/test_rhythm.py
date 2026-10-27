"""M2: midday/evening updates and the weekly review (rhythm.py)."""

import time
from dataclasses import dataclass, replace
from datetime import datetime

import pytest

from personal_agent.config import Config, ConfigError
from personal_agent.rhythm import (RhythmScheduler, Snapshot, parse_slot_time, parse_weekly, update_lines,
                                   weekly_lines)


@dataclass(frozen=True)
class Mail:
    subject: str
    date: float
    seen: bool = False
    answered: bool = False
    is_list: bool = False
    is_auto: bool = False


def ts(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute).timestamp()  # 30.10.2026 is a Friday


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send_now(self, kind, build, key=None, exclude=()):
        self.sent.append((kind, build(None), key))
        return True


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PA_STATE_DIR", str(tmp_path / "state"))
    for name in ("PA_RHYTHM_MIDDAY", "PA_RHYTHM_EVENING", "PA_RHYTHM_WEEKLY", "PA_MODULES", "PA_BRIEF_TIME",
                 "PA_LANGUAGE", "PA_MAIL_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_parsing():
    assert parse_slot_time("12:30") == (12, 30) and parse_slot_time("") is None
    assert parse_weekly("Fri 16:30") == (4, 16, 30) and parse_weekly(None) is None
    for bad in ("25:00", "12:3", "noon"):
        with pytest.raises(ValueError):
            parse_slot_time(bad)
    with pytest.raises(ValueError):
        parse_weekly("someday 10:00")


def test_config_is_off_by_default_and_follows_the_brief_module(env):
    config = Config.from_env()
    assert (config.rhythm_midday, config.rhythm_evening, config.rhythm_weekly) == (None, None, None)
    env.setenv("PA_RHYTHM_MIDDAY", "12:30")
    env.setenv("PA_RHYTHM_WEEKLY", "fri 16:30")
    config = Config.from_env()
    assert config.rhythm_midday == (12, 30) and config.rhythm_weekly == (4, 16, 30)
    env.setenv("PA_MODULES", "telegram")
    assert Config.from_env().rhythm_midday is None
    env.setenv("PA_MODULES", "brief")
    env.setenv("PA_RHYTHM_EVENING", "late")
    with pytest.raises(ConfigError, match="PA_RHYTHM"):
        Config.from_env()


def test_update_counts_new_and_waiting_mail_and_tasks():
    mails = [Mail("Mahnung", ts(29, 11)), Mail("Anfrage Termin", ts(29, 10)), Mail("Rechnung", ts(29, 6), seen=True),
             Mail("Newsletter", ts(29, 11), is_list=True), Mail("Reklamation alt", ts(28, 9))]
    snap = Snapshot(mails, ["work: login failed"], {"done": 2, "failed": 1, "running": 0, "queued": 1})
    lines = update_lines(snap, ts(29, 8), "08:00")
    assert lines[0] == "Mail since 08:00: 3 new (deadline/legal 1, request 1, newsletter/automatic 1)"
    assert lines[1] == "Still unread and waiting for you: deadline/legal 1, complaint 1, request 1"
    assert lines[2].startswith("Background tasks: 2 done, 1 failed")
    assert lines[3] == "Check: work: login failed"


def test_weekly_review_counts_the_process_not_people():
    mails = [Mail("Anfrage 1", ts(27, 9), answered=True), Mail("Anfrage 2", ts(28, 9)),
             Mail("Rechnung", ts(29, 9)), Mail("Alt", ts(20, 9))]
    lines = weekly_lines(Snapshot(mails, [], {}), ts(26, 0), "de")
    assert lines[0] == "Mail diese Woche: 3 (Rechnung/Zahlung 1, Anfrage 2)"
    assert lines[1] == "Anfragen beantwortet: 1 von 2"


def make(env, clock, **settings):
    for key, value in settings.items():
        env.setenv(key, value)
    notifier = FakeNotifier()
    snap = Snapshot([Mail("Anfrage", clock.now - 600)], [], {})
    return RhythmScheduler(Config.from_env(), notifier=notifier, clock=clock, snapshot=lambda c, n: snap), notifier


def test_slot_is_sent_once_and_survives_a_restart(env):
    clock = Clock(ts(29, 12, 30))
    sched, notifier = make(env, clock, PA_RHYTHM_MIDDAY="12:30", PA_BRIEF_TIME="07:30")
    assert sched.tick() == ["midday"]
    assert notifier.sent[0][0] == "brief" and notifier.sent[0][1].startswith("Midday update 12:30\nMail since 07:30")
    clock.now += 60
    assert sched.tick() == []
    restarted, notifier2 = make(env, clock)
    assert restarted.tick() == [] and notifier2.sent == []


def test_slot_outside_the_grace_window_is_skipped(env):
    clock = Clock(ts(29, 14, 0))
    sched, notifier = make(env, clock, PA_RHYTHM_MIDDAY="12:30")
    assert sched.tick() == [] and notifier.sent == []


def test_evening_counts_since_the_midday_update(env):
    clock = Clock(ts(29, 12, 30))
    sched, notifier = make(env, clock, PA_RHYTHM_MIDDAY="12:30", PA_RHYTHM_EVENING="18:00")
    sched.tick()
    clock.now = ts(29, 18, 5)
    assert sched.tick() == ["evening"]
    assert "Mail since 12:30" in notifier.sent[-1][1]


def test_weekly_only_on_its_day(env):
    clock = Clock(ts(29, 16, 31))  # Thursday
    sched, notifier = make(env, clock, PA_RHYTHM_WEEKLY="fri 16:30")
    assert sched.tick() == []
    clock.now = ts(30, 16, 31)
    assert sched.tick() == ["weekly"] and notifier.sent[0][1].startswith("Weekly review, week 44")
    clock.now += 120
    assert sched.tick() == []


def test_inactive_scheduler_does_nothing(env):
    sched, notifier = make(env, Clock(ts(29, 12, 30)))
    assert not sched.active and sched.tick() == []
