"""B9: audit log of approval decisions (audit.py, `personal-agent audit`)."""

import json
import os
import stat
import time

from personal_agent import audit
from personal_agent.audit import AUDIT_FILE, format_entry, read_audit, record_approval
from personal_agent.cli import main

NOW = time.mktime((2026, 11, 14, 15, 0, 0, 0, 0, -1))


def test_entries_hold_decision_but_never_input_and_stay_private(tmp_path):
    record_approval(tmp_path, scope="main", channel="telegram", tool="Write", level=2, allow=True,
                    reason="approved by the person", now=NOW)
    record_approval(tmp_path, scope="task-abc123", channel="chat", tool="Bash; rm -rf /", level=3, allow=False,
                    reason="the person did not approve\nthis action ‮", now=NOW + 1)
    entries = read_audit(tmp_path)
    assert entries[0] == {"ts": NOW, "action": "approval", "scope": "main", "channel": "telegram", "tool": "Write",
                          "level": 2, "decision": "allow", "reason": "approved by the person"}
    assert entries[1]["tool"] == "?" and entries[1]["reason"] == "the person did not approve?this action ?"
    assert stat.S_IMODE(os.stat(tmp_path / AUDIT_FILE).st_mode) == 0o600
    assert format_entry(entries[0]).endswith("allow  level 2  Write  [main via telegram]  approved by the person")


def test_log_is_capped_and_broken_lines_are_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "MAX_BYTES", 3000)
    monkeypatch.setattr(audit, "KEEP_LINES", 5)
    for i in range(40):
        record_approval(tmp_path, scope="main", channel="chat", tool="Edit", level=2, allow=True, reason="ok",
                        now=NOW + i)
    with open(tmp_path / AUDIT_FILE, "a") as f:
        f.write("not json\n" + json.dumps(["x"]) + "\n")
    entries = read_audit(tmp_path, last=1000)
    assert len(entries) < 40 and entries[-1]["ts"] == NOW + 39
    assert [e["ts"] for e in read_audit(tmp_path, last=2)] == [NOW + 38, NOW + 39]


def test_cli_shows_the_newest_lines(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PA_STATE_DIR", str(tmp_path / "state"))
    assert main(["audit"]) == 0
    assert "no approvals recorded yet" in capsys.readouterr().out
    record_approval(tmp_path / "state", scope="main", channel="chat", tool="Write", level=2, allow=False,
                    reason="approval timed out", now=NOW)
    assert main(["audit", "--last", "5"]) == 0
    assert "deny  level 2  Write  [main via chat]  approval timed out" in capsys.readouterr().out
