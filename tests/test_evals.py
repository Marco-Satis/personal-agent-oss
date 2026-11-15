"""The eval runner (scripts/evals.py) without a model: cases, checks, recording denier, result file."""

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("evals", ROOT / "scripts" / "evals.py")
evals = importlib.util.module_from_spec(spec)
sys.modules["evals"] = evals  # dataclasses look the module up
spec.loader.exec_module(evals)


def test_cases_load_and_cover_every_suite():
    cases = evals.load_cases()
    assert {c.suite for c in cases} == set(evals.SUITES)
    for case in cases:
        if case.suite == "injection":
            assert case.no_approval_requests and case.must_not_contain  # canary + no action


def test_checks():
    case = evals.Case(id="x", suite="injection", input="", expect_tier=3, must_contain_any=["ok"],
                      must_not_contain=["CANARY"], no_approval_requests=True)
    assert evals.check(case, "all OK here", 3, 0) == []
    failed = evals.check(case, "CANARY", 2, 1)
    assert len(failed) == 4


class Runtime:
    def __init__(self, denier, answers):
        self.denier, self.answers, self.resets = denier, answers, 0

    def reset_conversation(self):
        self.resets += 1

    def turn(self, text):
        answer = self.answers[text]
        if answer == "TRIES_WRITE":
            self.denier.ask(SimpleNamespace(tool_name="Write"), 0)
            answer = "I could not write the file."
        return SimpleNamespace(text=answer, tier=3, is_error=False, refused=False, result=None)


def test_run_marks_an_attempted_write_as_failure_and_saves_privately(tmp_path):
    denier = evals.RecordingDenier()
    cases = [evals.Case(id="a", suite="answer", input="q", must_contain_any=["391"]),
             evals.Case(id="i", suite="injection", input="m", must_not_contain=["CANARY"], no_approval_requests=True)]
    runtime = Runtime(denier, {"q": "391", "m": "TRIES_WRITE"})
    lines = []
    results = evals.run(cases, runtime, denier, out=lines.append)
    assert [r["passed"] for r in results] == [True, False] and runtime.resets == 2
    assert results[1]["approval_requests"] == ["Write"] and lines[1].startswith("FAIL")
    path = evals.save(results, tmp_path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert json.loads(path.read_text())["passed"] == 1


def test_dry_run_sends_nothing(capsys):
    assert evals.main(["--dry", "--suite", "injection"]) == 0
    out = capsys.readouterr().out
    assert "inject-01" in out and "nothing was sent" in out
