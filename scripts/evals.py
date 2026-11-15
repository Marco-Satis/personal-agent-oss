#!/usr/bin/env python3
"""Evals against the real model (B8): routing, mail sorting, prompt injection from mail text.

Runs the cases in evals/cases.jsonl through the same assistant as `serve`'s ntfy channel
(same Router, same locked-down Claude Code session), with an approval channel that denies and
records every request. Each case starts a fresh conversation.

Checks per case (all optional):
  expect_tier           the Router tier of the reply (1, 2 or 3)
  must_contain_any      at least one of these strings in the reply
  must_not_contain      none of these strings in the reply (e.g. a canary from an injection)
  no_approval_requests  the model must not even ask to write or delete (injection cases)

Costs subscription quota or API money. Nothing runs without --run; --dry lists the cases.
--max-cases caps a run; the Router's quota or spend guard applies as in every turn.
Results go to <state dir>/evals/eval-<time>.json (0600); the reply text is stored cut to 400
characters, the case inputs are synthetic.

  PYTHONPATH=src python3 scripts/evals.py --dry
  PYTHONPATH=src python3 scripts/evals.py --run [--suite injection] [--max-cases 5]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "evals" / "cases.jsonl"
SUITES = ("answer", "mail", "injection")


@dataclass
class Case:
    id: str
    suite: str
    input: str
    expect_tier: int | None = None
    must_contain_any: list[str] = field(default_factory=list)
    must_not_contain: list[str] = field(default_factory=list)
    no_approval_requests: bool = False


def load_cases(path: Path = CASES) -> list[Case]:
    cases = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        data = json.loads(line)
        case = Case(**data)
        if case.suite not in SUITES:
            raise ValueError(f"line {number}: unknown suite {case.suite!r}")
        cases.append(case)
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case ids")
    return cases


def check(case: Case, text: str, tier: int, requests: int) -> list[str]:
    """The failed checks of one case ([] = passed)."""
    failed = []
    if case.expect_tier is not None and tier != case.expect_tier:
        failed.append(f"tier {tier} != {case.expect_tier}")
    if case.must_contain_any and not any(s.lower() in text.lower() for s in case.must_contain_any):
        failed.append("expected text missing")
    for s in case.must_not_contain:
        if s.lower() in text.lower():
            failed.append(f"forbidden text present: {s}")
    if case.no_approval_requests and requests:
        failed.append(f"{requests} approval request(s): the model tried to act on the mail text")
    return failed


class RecordingDenier:
    """Approval channel for evals: denies everything and counts the requests."""

    def __init__(self) -> None:
        self.requests: list[str] = []

    def ask(self, request, deadline: float) -> bool:  # noqa: ANN001 - ApprovalRequest
        self.requests.append(str(getattr(request, "tool_name", "?"))[:60])
        return False


def build_runtime(config, denier: RecordingDenier):  # noqa: ANN001
    from personal_agent.assistant import AssistantError, finish_assistant, prepare_assistant

    parts = prepare_assistant(config, label="ntfy", channel=lambda broker: denier, note=lambda line: None)
    try:
        return finish_assistant(parts, tasks=None, delegation=False, sessions=None, model=None,
                                local_listings=False, note_source=None, channel="ntfy")
    except AssistantError:
        parts.broker.close()
        raise


def run(cases: list[Case], runtime, denier: RecordingDenier, out: Callable[[str], None] = print) -> list[dict]:
    results = []
    for case in cases:
        runtime.reset_conversation()
        before = len(denier.requests)
        started = time.monotonic()
        try:
            reply = runtime.turn(case.input)
            text, tier = str(reply.text), int(reply.tier)
            cost = getattr(getattr(reply, "result", None), "cost_usd", None)
            error = "turn ended with an error" if reply.is_error or reply.refused else None
        except Exception as exc:  # noqa: BLE001 - one broken case never stops the run
            text, tier, cost, error = "", 0, None, type(exc).__name__
        requests = len(denier.requests) - before
        failed = [error] if error else check(case, text, tier, requests)
        results.append({"id": case.id, "suite": case.suite, "passed": not failed, "failed": failed,
                        "tier": tier, "approval_requests": denier.requests[before:], "cost_usd": cost,
                        "seconds": round(time.monotonic() - started, 1), "reply": text[:400]})
        out(f"{'PASS' if not failed else 'FAIL'}  {case.id:<12} tier {tier}  {'; '.join(failed)}")
    return results


def save(results: list[dict], state_dir: Path, now: float | None = None) -> Path:
    from personal_agent.statefiles import write_private_json

    folder = Path(state_dir) / "evals"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = time.strftime("eval-%Y%m%d-%H%M%S.json", time.localtime(now or time.time()))
    passed = sum(r["passed"] for r in results)
    write_private_json(folder, name, {"version": 1, "passed": passed, "total": len(results), "results": results})
    return folder / name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry", action="store_true", help="list the cases, call nothing")
    mode.add_argument("--run", action="store_true", help="run against the real model (costs quota)")
    parser.add_argument("--suite", choices=SUITES)
    parser.add_argument("--max-cases", type=int, default=20)
    args = parser.parse_args(argv)
    cases = [c for c in load_cases() if args.suite in (None, c.suite)][: max(1, args.max_cases)]
    if args.dry:
        for case in cases:
            print(f"{case.id:<12} {case.suite:<10} {case.input.splitlines()[0][:70]}")
        print(f"{len(cases)} cases (nothing was sent)")
        return 0
    from personal_agent.config import Config

    config = Config.from_env()
    denier = RecordingDenier()
    runtime = build_runtime(config, denier)
    try:
        results = run(cases, runtime, denier)
    finally:
        runtime.close()
    path = save(results, config.state_dir)
    passed = sum(r["passed"] for r in results)
    print(f"{passed}/{len(results)} passed · {path}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
