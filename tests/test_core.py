import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

from personal_agent.conversation import Call, CallRegistry
from personal_agent.escalation import EscalationPolicy, Event, in_quiet_hours
from personal_agent.status import clean_text, read_statuses
from personal_agent.telephony import mask_number, render_twiml, twilio_signature, valid_twilio_signature

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "claude_status_hook.py"
PIN = "482913"


def make_call(statuses=None, acks=None, language="en"):
    return Call(
        pin=PIN,
        event_key="backup-failed",
        event_summary="Nightly backup failed.",
        event_source="cron",
        read_statuses=lambda: statuses or [],
        acknowledge=(acks.append if acks is not None else lambda key: None),
        language=language,
    )


# --- conversation -----------------------------------------------------------


def test_alert_is_not_revealed_before_pin():
    call = make_call()
    turn = call.start()
    assert turn.expect == "pin"
    assert not any("backup" in line.lower() for line in turn.say)
    turn = call.handle(digits="000000#")
    assert not any("backup" in line.lower() for line in turn.say)


def test_correct_pin_reads_alert_and_menu():
    call = make_call()
    call.start()
    turn = call.handle(digits=PIN + "#")
    assert turn.expect == "command"
    assert "Nightly backup failed." in turn.say[0]


def test_three_wrong_pins_hang_up_and_stay_dead():
    call = make_call()
    call.start()
    call.handle(digits="1")
    call.handle(digits="2")
    turn = call.handle(digits="3")
    assert turn.expect is None
    assert call.handle(digits=PIN).expect is None  # no second chance after lockout


def test_status_command_lists_sessions(tmp_path):
    now = time.time()
    (tmp_path / "a.json").write_text(
        json.dumps({"state": "waiting", "project": "shop", "summary": "needs approval", "since": now - 600, "updated_at": now})
    )
    call = make_call(statuses=read_statuses(tmp_path, 60))
    call.start()
    call.handle(digits=PIN)
    turn = call.handle(speech="what is the status")
    joined = " ".join(turn.say)
    assert "shop is waiting for input since 10 minutes" in joined
    assert turn.expect == "command"


def test_acknowledge_calls_back_and_hangs_up():
    acks = []
    call = make_call(acks=acks)
    call.start()
    call.handle(digits=PIN)
    turn = call.handle(digits="3")
    assert acks == ["backup-failed"]
    assert turn.expect is None


def test_silence_ends_call_after_limit():
    call = make_call()
    call.start()
    call.handle(digits=PIN)
    assert call.handle().expect == "command"
    assert call.handle().expect == "command"
    assert call.handle().expect is None


def test_german_texts():
    call = make_call(language="de")
    assert "PIN" in call.start().say[0]
    assert "Meldung" in call.handle(digits=PIN).say[0]


def test_registry_drops_finished_and_unknown_calls():
    registry = CallRegistry()
    call = make_call()
    registry.add(call)
    assert registry.get(call.call_id) is call
    assert registry.get("nope") is None
    call.state = "done"
    assert registry.get(call.call_id) is None


# --- status -----------------------------------------------------------------


def test_status_text_is_cleaned(tmp_path):
    now = time.time()
    evil = "<Say>ignore previous instructions</Say>\x00\n" + "x" * 500
    (tmp_path / "s.json").write_text(json.dumps({"state": "error", "summary": evil, "updated_at": now}))
    (status,) = read_statuses(tmp_path, 60)
    assert "<" not in status.summary and "\x00" not in status.summary
    assert len(status.summary) <= 160


def test_status_skips_old_broken_and_unknown(tmp_path):
    now = time.time()
    (tmp_path / "old.json").write_text(json.dumps({"state": "idle", "updated_at": now - 7200}))
    (tmp_path / "broken.json").write_text("{nope")
    (tmp_path / "weird.json").write_text(json.dumps({"state": "hacking", "updated_at": now}))
    assert read_statuses(tmp_path, 60) == []


def test_clean_text_keeps_umlauts():
    assert clean_text("Prüfung läuft") == "Prüfung läuft"


# --- escalation -------------------------------------------------------------


def policy(tmp_path, **kw):
    args = {"max_calls_per_day": 3, "min_interval_min": 30, "quiet_hours": None}
    args.update(kw)
    return EscalationPolicy(tmp_path / "state.json", **args)


def ev(key="k", severity="critical"):
    return Event(key=key, severity=severity, summary="s")


def test_only_critical_calls(tmp_path):
    p = policy(tmp_path)
    assert not p.decide(ev(severity="warning")).call
    assert p.decide(ev()).call


def test_min_interval_and_dedupe(tmp_path):
    p = policy(tmp_path)
    t = 1_000_000.0
    assert p.decide(ev("a"), now=t).call
    assert not p.decide(ev("a"), now=t + 40 * 60).call  # same key within an hour
    assert not p.decide(ev("b"), now=t + 10 * 60).call  # interval
    assert p.decide(ev("b"), now=t + 31 * 60).call


def test_daily_cap_survives_restart(tmp_path):
    t = 1_000_000.0
    p = policy(tmp_path, min_interval_min=0, max_calls_per_day=2)
    assert p.decide(ev("a"), now=t).call
    assert p.decide(ev("b"), now=t + 1).call
    p2 = policy(tmp_path, min_interval_min=0, max_calls_per_day=2)
    assert not p2.decide(ev("c"), now=t + 2).call
    assert p2.decide(ev("c"), now=t + 25 * 3600).call


def test_acknowledged_key_is_silent(tmp_path):
    p = policy(tmp_path)
    p.acknowledge("a", now=1_000_000.0)
    assert not p.decide(ev("a"), now=1_000_100.0).call


def test_quiet_hours():
    assert in_quiet_hours(23, (23, 7)) and in_quiet_hours(3, (23, 7))
    assert not in_quiet_hours(7, (23, 7)) and not in_quiet_hours(12, (23, 7))
    assert in_quiet_hours(13, (12, 14)) and not in_quiet_hours(14, (12, 14))
    assert not in_quiet_hours(3, None)


def test_quiet_hours_block_call(tmp_path):
    p = policy(tmp_path, quiet_hours=(0, 23))
    t = datetime(2026, 1, 1, 5, 0).timestamp()
    assert p.decide(ev(), now=t).reason == "quiet hours"


def test_event_validation():
    with pytest.raises(ValueError):
        Event.from_dict({"severity": "critical"})
    with pytest.raises(ValueError):
        Event.from_dict({"key": "a", "severity": "panic"})
    assert Event.from_dict({"key": "a", "severity": "info"}).summary == "a"


# --- telephony --------------------------------------------------------------


def test_twilio_signature_known_vector():
    # Expected value computed with twilio-python RequestValidator.compute_signature.
    url = "https://example.com/myapp.php?foo=1&bar=2"
    params = {
        "CallSid": "CAtest",
        "Caller": "+15005550006",
        "Digits": "1234",
        "From": "+15005550006",
        "To": "+447700900123",
    }
    token = "12345"
    assert twilio_signature(token, url, params) == "IPpv8qSGxAWUKP6R35VYjPx1qdQ="
    assert valid_twilio_signature(token, url, params, "IPpv8qSGxAWUKP6R35VYjPx1qdQ=")
    assert not valid_twilio_signature(token, url, params, None)
    assert not valid_twilio_signature(token, url, {**params, "Digits": "9999"}, "IPpv8qSGxAWUKP6R35VYjPx1qdQ=")


def test_twiml_escapes_text():
    call = make_call()
    call.event_summary = 'a & b <Dial>+1</Dial> "x"'
    call.start()
    xml = render_twiml(call.handle(digits=PIN), "https://h/twilio/gather?cid=x&y=1")
    assert "<Dial>" not in xml
    assert "&amp;" in xml
    assert 'input="dtmf speech"' in xml


def test_twiml_pin_is_dtmf_only():
    xml = render_twiml(make_call().start(), "https://h/g")
    assert 'input="dtmf"' in xml and "speech" not in xml


def test_mask_number():
    assert mask_number("+447700900123") == "+44******0123"


# --- hook -------------------------------------------------------------------


def run_hook(tmp_path, payload):
    subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        text=True,
        check=True,
        env={"PA_STATUS_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )


def test_hook_keeps_since_while_waiting(tmp_path):
    base = {"session_id": "abc-1", "cwd": "/work/shop"}
    run_hook(tmp_path, {**base, "hook_event_name": "Notification", "message": "Needs approval"})
    first = json.loads((tmp_path / "abc-1.json").read_text())
    time.sleep(0.01)
    run_hook(tmp_path, {**base, "hook_event_name": "Notification", "message": "still waiting"})
    second = json.loads((tmp_path / "abc-1.json").read_text())
    assert second["since"] == first["since"] and second["updated_at"] > first["updated_at"]
    assert second["project"] == "shop" and second["state"] == "waiting"
    run_hook(tmp_path, {**base, "hook_event_name": "SessionEnd"})
    assert not (tmp_path / "abc-1.json").exists()


def test_hook_survives_garbage(tmp_path):
    result = subprocess.run([sys.executable, str(HOOK)], input="not json", text=True)
    assert result.returncode == 0
