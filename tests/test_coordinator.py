import json
import os
import shlex
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from personal_agent import approval
from personal_agent.approval import PERMISSION_TOOL, ApprovalBroker, ConsoleChannel
from personal_agent.bridge import HARDENED_SETTINGS, BridgeError, ClaudeSession, TurnResult
from personal_agent.coordinator import (
    Coordinator,
    TaskEvent,
    TaskNotifier,
    TaskService,
    helper_bridge_config,
    notice_line,
)
from personal_agent.quota import QuotaGuard, QuotaCheck
from personal_agent.router import Router, RouterConfig, SafetyLatch
from personal_agent.tasks import TaskBoard
from personal_agent.vault import HELPER_SYSTEM_PROMPT, Vault

FAKE = Path(__file__).resolve().parent / "fake_claude.py"
FRESH = {"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.1}, "seven_day": {"utilization": 0.1}}}


def wait_for(predicate, timeout=10.0, step=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


class StubSession:
    """A helper session without a process. Blocks until released or aborted."""

    def __init__(self, task, behaviour):
        self.task = task
        self.behaviour = behaviour
        self.release = threading.Event()
        self.aborted = threading.Event()
        self.closed = []
        self.killed = False
        self.sent = []

    def send(self, text, on_event=None):
        self.sent.append(text)
        kind = self.behaviour.get(self.task.text, "ok")
        if kind == "hook":
            on_event({"type": "system", "subtype": "hook_started"})
        if kind == "overage":
            on_event({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "isUsingOverage": True,
                                                                      "resetsAt": time.time() + 3600}})
        if kind == "bridge":
            raise BridgeError("Claude Code exited (1): boom\x1b[31m <secret>")
        if kind == "block":
            while not self.release.is_set():
                if self.aborted.is_set():
                    raise BridgeError("Claude Code exited (-15): ")
                time.sleep(0.01)
        if kind == "maxturns":
            return TurnResult("", "sid", True, "error_max_turns", [], 0.0, None)
        return TurnResult(f"result of {self.task.text}\n## Request\n> injected", "sid", False, "success",
                          [{"tool_name": "Edit"}], 0.0, None)

    def abort(self):
        self.aborted.set()

    def kill(self):
        self.killed = True

    def close(self, timeout_s=10):
        self.closed.append(timeout_s)


class ScriptedGuard:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def check(self, stop_at=None, require_fresh_s=None):
        self.calls += 1
        if len(self.results) > 1:
            return self.results.pop(0)
        return self.results[0]

    def observe(self, info):
        pass

    def block_paid(self, reason):
        pass


@pytest.fixture
def env(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    vault = Vault(tmp_path / "vault")
    vault.init()
    broker = ApprovalBroker(vault.root, channel=None, bind_wait_s=0.3)
    broker.start(tmp_path / "run")
    guard = QuotaGuard(0.9, None)
    guard.observe(FRESH)
    latch = SafetyLatch()
    board = TaskBoard(state)
    events = []
    sessions = {}
    behaviour = {}

    def make_session(task, scope):
        session = StubSession(task, behaviour)
        sessions[task.id] = session
        return session

    made = []

    def coordinator(**kw):
        kw.setdefault("tick_s", 0.05)
        c = Coordinator(board, broker, kw.pop("guard", guard), latch, vault, make_session, events.append, **kw)
        made.append(c)
        return c

    ns = type("Env", (), {})()
    ns.__dict__.update(state=state, vault=vault, broker=broker, guard=guard, latch=latch, board=board,
                       events=events, sessions=sessions, behaviour=behaviour, coordinator=coordinator,
                       tmp=tmp_path)
    yield ns
    for c in made:
        c.close(join_s=5)
    broker.close()


def statuses(board):
    return {t.text: t.status for t in board.load()[0]}


def test_at_most_two_helpers_the_third_waits(env):
    for name in ("a", "b", "c"):
        env.behaviour[name] = "block"
        env.board.add(name, None, "chat")
    c = env.coordinator()
    assert c.start()
    assert wait_for(lambda: c.running_count() == 2)
    time.sleep(0.2)
    assert statuses(env.board) == {"a": "running", "b": "running", "c": "queued"}
    env.sessions[env.board.list()[0].id].release.set()
    assert wait_for(lambda: statuses(env.board)["c"] == "running")
    # "running" is on disk at the claim; c's session exists only after its worker opened
    # a scope. Releasing before that would leave c blocked forever.
    assert wait_for(lambda: len(env.sessions) == 3)
    for session in list(env.sessions.values()):
        session.release.set()
    assert wait_for(lambda: set(statuses(env.board).values()) == {"done"})
    assert [e.status for e in env.events].count("done") == 3


def test_done_writes_the_result_file(env):
    task = env.board.add("summarise", None, "chat")
    c = env.coordinator()
    c.start()
    assert wait_for(lambda: env.board.get(task.id).status == "done")
    record = env.board.get(task.id)
    text = env.vault.read_task_result(task.id)
    assert record.result == f"outputs/tasks/{task.id}.md" and record.denials == 1
    import hashlib

    assert hashlib.sha256(text.encode()).hexdigest() == record.result_sha256
    assert text.startswith(f"# Task {task.id}\n") and text.count("\n## Request\n") == 1
    assert "result of summarise" in text and "\\## Request" in text
    event = env.events[-1]
    assert notice_line(event) == f"[task {task.id} done: outputs/tasks/{task.id}.md]"
    sent = env.sessions[task.id].sent[0]
    assert sent.startswith(f'<task id="{task.id}">\n<user-message>\nsummarise')


def test_quota_over_the_task_threshold_waits_and_resumes(env):
    env.guard.observe({"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.85,
                                                                              "resetsAt": time.time() + 3600}}})
    assert env.guard.check().ok  # the chat itself may still send
    task = env.board.add("later", None, "chat")
    c = env.coordinator(task_quota_stop=0.8)
    c.start()
    assert wait_for(lambda: env.board.get(task.id).status == "waiting")
    time.sleep(0.3)
    waiting = [e for e in env.events if e.status == "waiting"]
    assert len(waiting) == 1 and "85 %" in waiting[0].error  # notified once
    assert "85 %" in env.board.get(task.id).error
    env.guard.observe(FRESH)
    assert wait_for(lambda: env.board.get(task.id).status == "done")


def test_worker_rechecks_the_quota_before_sending(env):
    ok, blocked = QuotaCheck(True, "within quota"), QuotaCheck(False, "five-hour window at 95 %")
    guard = ScriptedGuard([ok, blocked, blocked])
    task = env.board.add("x", None, "chat")
    c = env.coordinator(guard=guard)
    c.start()
    assert wait_for(lambda: env.board.get(task.id).status == "waiting")
    assert env.sessions[task.id].sent == []


def test_no_fresh_quota_data_means_one_helper_at_a_time(env):
    guard = QuotaGuard(0.9, None)  # no data yet
    for name in ("a", "b"):
        env.behaviour[name] = "block"
        env.board.add(name, None, "chat")
    c = env.coordinator(guard=guard)
    c.start()
    assert wait_for(lambda: c.running_count() == 1)
    time.sleep(0.3)
    assert c.running_count() == 1
    for session in list(env.sessions.values()):
        session.release.set()


def test_safety_stop_trips_the_latch_and_stops_every_helper(env):
    env.behaviour.update(calm="block", hook="hook")
    calm = env.board.add("calm", None, "chat")
    c = env.coordinator()
    c.start()
    assert wait_for(lambda: c.running_count() == 1)
    hook = env.board.add("hook", None, "chat")
    c.wake()
    assert wait_for(lambda: env.board.get(hook.id).status == "failed")
    assert wait_for(lambda: env.board.get(calm.id).status == "failed")
    assert env.latch.reason and "hook" in env.latch.reason
    assert "hook ran" in env.board.get(hook.id).error
    assert env.board.get(calm.id).error == "stopped: safety stop elsewhere"
    assert wait_for(lambda: env.broker.scopes() == ["main"])
    assert not list(env.broker.runtime_dir.glob("mcp-task-*.json"))
    queued = env.board.add("after", None, "chat")
    c.wake()
    assert wait_for(lambda: env.board.get(queued.id).status == "waiting")
    assert "paused" in env.board.get(queued.id).error

    class Main:
        def send(self, text, on_event=None):  # pragma: no cover - must never be called
            raise AssertionError("sent")

        def close(self, timeout_s=10):
            pass

    router = Router(Main(), None, env.guard, RouterConfig(), statuses=lambda: [], latch=env.latch)
    reply = router.handle("hello")
    assert reply.refused and reply.safety and "Restart the chat" in reply.text


def test_main_safety_stop_stops_running_helpers(env):
    env.behaviour["slow"] = "block"
    task = env.board.add("slow", None, "chat")
    c = env.coordinator()
    c.start()
    assert wait_for(lambda: c.running_count() == 1)
    env.latch.trip("a hook ran inside the assistant session")  # e.g. from the main session's router
    assert wait_for(lambda: env.board.get(task.id).status == "failed")
    assert env.sessions[task.id].aborted.is_set()


def test_paid_usage_fails_the_task_without_tripping_the_latch(env):
    env.behaviour["paid"] = "overage"
    task = env.board.add("paid", None, "chat")
    c = env.coordinator()
    c.start()
    assert wait_for(lambda: env.board.get(task.id).status == "failed")
    assert "overage" in env.board.get(task.id).error and env.latch.reason is None


def test_cancel_queued_and_running(env):
    env.behaviour.update(run="block", queued="block")
    running = env.board.add("run", None, "chat")
    c = env.coordinator(max_parallel=1)
    c.start()
    assert wait_for(lambda: c.running_count() == 1)
    queued = env.board.add("queued", None, "chat")
    service = TaskService(env.board, c, env.latch, env.vault)
    assert service.cancel_text(queued.id) == f"task {queued.id} cancelled"
    assert service.cancel_text(running.id[:4]) == f"task {running.id}: stopping …"
    assert wait_for(lambda: env.board.get(running.id).status == "cancelled")
    assert env.sessions[running.id].aborted.is_set()
    assert notice_line([e for e in env.events if e.task_id == running.id][-1]) == f"[task {running.id} cancelled]"
    assert queued.id not in env.sessions  # never started


def test_watchdog_times_out(env):
    env.behaviour["hang"] = "block"
    task = env.board.add("hang", None, "chat")
    c = env.coordinator(task_timeout_s=0.3)
    c.start()
    assert wait_for(lambda: env.board.get(task.id).status == "failed")
    assert env.board.get(task.id).error == "timed out after 0.3 s"


def test_bridge_error_is_cleaned_and_max_turns_is_named(env):
    env.behaviour.update(bridge="bridge", turns="maxturns")
    a = env.board.add("bridge", None, "chat")
    b = env.board.add("turns", None, "chat")
    c = env.coordinator()
    c.start()
    assert wait_for(lambda: env.board.get(b.id).status == "failed" and env.board.get(a.id).status == "failed")
    error = env.board.get(a.id).error
    assert error.startswith("Claude Code failed: ") and "\x1b" not in error and "<" not in error
    assert env.board.get(b.id).error == "turn limit reached"


def test_result_write_failure_fails_the_task(env):
    outputs = env.vault.root / "outputs"
    (env.tmp / "elsewhere").mkdir()
    (outputs / "tasks").symlink_to(env.tmp / "elsewhere")
    task = env.board.add("x", None, "chat")
    c = env.coordinator()
    c.start()
    assert wait_for(lambda: env.board.get(task.id).status == "failed")
    assert env.board.get(task.id).error == "could not write the result"
    assert list((env.tmp / "elsewhere").iterdir()) == []


def test_close_interrupts_running_tasks_and_releases_the_owner_lock(env):
    env.behaviour["long"] = "block"
    task = env.board.add("long", None, "chat")
    c = env.coordinator()
    c.start()
    assert wait_for(lambda: c.running_count() == 1)
    c.close(join_s=5)
    record = env.board.get(task.id)
    assert record.status == "failed" and record.error == "interrupted (the chat was closed)"
    other = TaskBoard(env.state)
    assert other.acquire_owner()
    other.release_owner()


def test_second_coordinator_is_not_the_owner(env):
    first = env.coordinator()
    assert first.start()
    other_board = TaskBoard(env.state)  # the second chat process has its own board object
    second = Coordinator(other_board, env.broker, env.guard, env.latch, env.vault, lambda t, s: None,
                         env.events.append)
    assert not second.start() and not second.owner
    service = TaskService(other_board, None, env.latch, env.vault)
    _task, line = service.add("from the second chat", None)
    assert line.endswith("it runs in the other open chat; its approvals appear there")


def test_expired_and_daily_limit(env):
    clock_board = TaskBoard(env.state, clock=lambda: time.time() - 48 * 3600)
    old = clock_board.add("old", None, "chat")
    env.board.max_starts_per_day = 1
    first = env.board.add("first", None, "chat")
    second = env.board.add("second", None, "chat")
    c = env.coordinator()
    c.start()
    assert wait_for(lambda: env.board.get(first.id).status == "done")
    assert env.board.get(old.id).error == "expired before it ran"
    assert wait_for(lambda: env.board.get(second.id).status == "waiting")
    assert "daily task limit" in env.board.get(second.id).error


def test_service_lines_and_show(env):
    service = TaskService(env.board, None, env.latch, env.vault)
    task, line = service.add("summarise the backup options", "research")
    assert line.startswith(f"task {task.id} queued (0 running, 0 ahead) · result: outputs/tasks/{task.id}.md")
    assert (env.vault.root / "wiki" / "domains" / "research.md").read_text().startswith("# Domain: research")
    listing = service.list_text()
    assert task.id in listing and "research" in listing and "queued" in listing
    delegated_id = service.delegate("look something up", None)
    assert "[delegated]" in service.list_text()
    assert "[delegated by the assistant]" in service.show_text(delegated_id)
    shown = service.show_text(task.id[:4])
    assert "status: queued" in shown and "domain: research" in shown
    env.latch.trip("a hook ran")
    assert "tasks are paused: a hook ran" in service.add("one more", None)[1]
    text, is_error = service.command(type("C", (), {"kind": "task_show", "arg": "ffff"})())
    assert is_error and "no task" in text


def test_domain_symlink_refuses_the_add(env):
    domains = env.vault.root / "wiki" / "domains"
    domains.mkdir(parents=True)
    (domains / "evil.md").symlink_to(env.tmp / "target.md")
    service = TaskService(env.board, None, env.latch, env.vault)
    text, is_error = service.command(type("C", (), {"kind": "task_add", "arg": "x", "domain": "evil"})())
    assert is_error and "unsafe" in text and env.board.load()[0] == []


def test_show_flags_a_changed_result_file(env):
    task = env.board.add("summarise", None, "chat")
    c = env.coordinator()
    c.start()
    assert wait_for(lambda: env.board.get(task.id).status == "done")
    service = TaskService(env.board, c, env.latch, env.vault)
    shown = service.show_text(task.id)
    assert "[result preview: data written by a helper]" in shown and "changed" not in shown
    (env.vault.root / "outputs" / "tasks" / f"{task.id}.md").write_text("tampered")
    assert "(the file changed since the helper wrote it)" in service.show_text(task.id)


# --- Discord notices: counts only -----------------------------------------------------------


class Webhook:
    def __init__(self, ok=True):
        self.posts = []
        self.ok = ok

    def post(self, text):
        self.posts.append(text)
        return self.ok


def test_task_notifier_posts_counts_only(env):
    hook = Webhook()
    clock = [100.0]
    notifier = TaskNotifier(hook, env.board, min_interval_s=60, clock=lambda: clock[0], run_thread=False)
    task = env.board.add("Secret Title about my bank", "finance", "chat")
    notifier.record(TaskEvent(task.id, "done", f"outputs/tasks/{task.id}.md", None))
    notifier.record(TaskEvent("bbbbbb", "failed", None, "Claude Code failed: secret error"))
    assert notifier.flush()
    assert hook.posts == ["Tasks: 1 done, 1 failed, 0 cancelled · 0 running, 1 queued"]
    for forbidden in (task.id, "Secret", "finance", "outputs", "secret error", "bbbbbb"):
        assert forbidden not in hook.posts[0]
    notifier.record(TaskEvent("cccccc", "cancelled", None, None))
    assert not notifier.flush()  # coalesced: one post per interval
    clock[0] += 61
    assert notifier.flush() and hook.posts[-1].startswith("Tasks: 0 done, 0 failed, 1 cancelled")
    notifier.record(TaskEvent("dddddd", "done", None, None))
    notifier.close()  # flushes pending counts
    assert hook.posts[-1].startswith("Tasks: 1 done") and len(hook.posts) == 3


def test_failed_post_keeps_the_counts(env):
    hook = Webhook(ok=False)
    notifier = TaskNotifier(hook, None, run_thread=False)
    notifier.record(TaskEvent("aaaaaa", "done", None, None))
    assert not notifier.flush()
    hook.ok = True
    assert notifier.flush(force=True) and hook.posts[-1].startswith("Tasks: 1 done")


# --- end to end with the fake CLI and a real broker ----------------------------------------------


@pytest.fixture
def real(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    log = tmp_path / "fake.jsonl"
    wrapper = tmp_path / "claude"
    wrapper.write_text(
        f"#!/bin/sh\nFAKE_CLAUDE_LOG={shlex.quote(str(log))} exec "
        f"{shlex.quote(sys.executable)} {shlex.quote(str(FAKE))} \"$@\"\n"
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    vault = Vault(tmp_path / "vault")
    vault.init()
    monkeypatch.setattr(approval, "stdin_is_tty", lambda: True)
    monkeypatch.setattr(approval, "discard_typeahead", lambda: True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-reach-helpers")  # pragma: allowlist secret
    lines = []
    answers = {"value": "y"}
    broker = ApprovalBroker(vault.root, timeout_s=30, bind_wait_s=2)
    broker.channel = ConsoleChannel(threading.Lock(), read_line=lambda t: answers["value"], write=lines.append,
                                    stop=broker.closing.is_set, stop_for=broker.request_stopped)
    broker.start(tmp_path / "run")
    prompt = broker.write_runtime_file("helper-system-prompt.md", HELPER_SYSTEM_PROMPT)
    guard = QuotaGuard(0.9, None)
    guard.observe(FRESH)
    latch = SafetyLatch()
    board = TaskBoard(state)
    events = []

    def make_session(task, scope):
        cfg = helper_bridge_config(task, scope, workdir=vault.root, config_dir=tmp_path / "cfg",
                                   claude_bin=str(wrapper), model="sonnet", system_prompt_file=prompt,
                                   permission_tool=PERMISSION_TOOL, server_name="pa", oauth_token=None)
        return ClaudeSession(cfg)

    coordinator = Coordinator(board, broker, guard, latch, vault, make_session, events.append, tick_s=0.05)
    assert coordinator.start()

    def entries():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def starts():
        return [entry for entry in entries() if "args" in entry]

    def finishes():  # the ends of SLEEP commands
        return [entry for entry in entries() if "slept" in entry]

    ns = type("Real", (), {})()
    ns.__dict__.update(board=board, vault=vault, broker=broker, coordinator=coordinator, events=events,
                       lines=lines, answers=answers, starts=starts, finishes=finishes, latch=latch, prompt=prompt,
                       tmp=tmp_path)
    yield ns
    coordinator.close(join_s=10)
    broker.close()


def test_e2e_helper_appends_domain_notes_with_approval(real):
    service = TaskService(real.board, real.coordinator, real.latch, real.vault)
    task, _ = service.add("APPEND wiki/domains/research.md - 2026-10-18 (task {task}): restic is fine", "research")
    assert wait_for(lambda: real.board.get(task.id).status == "done", timeout=20)
    notes = (real.vault.root / "wiki" / "domains" / "research.md").read_text()
    assert f"- 2026-10-18 (task {task.id}): restic is fine" in notes
    assert f"[approval · task {task.id} (research)] level 2 (write): Edit" in real.lines
    result = real.vault.read_task_result(task.id)
    assert "## Request" in result and "## Result" in result and "appended" in result
    assert real.board.get(task.id).result_sha256
    [start] = real.starts()
    args = start["args"]
    assert args[args.index("--tools") + 1] == "Read,Glob,Grep,Edit"
    for tool in ("Skill", "Task", "Agent", "Write"):
        assert tool not in args[args.index("--tools") + 1].split(",")
    assert args[args.index("--settings") + 1] == HARDENED_SETTINGS
    assert "--no-session-persistence" in args and "--plugin-dir" not in args and "--restricted" in args
    assert args[args.index("--max-turns") + 1] == "30"
    mcp = Path(args[args.index("--mcp-config") + 1])
    assert mcp.name == f"mcp-task-{task.id}.json" and not mcp.exists()  # removed with the scope
    prompt = Path(args[args.index("--append-system-prompt-file") + 1])
    assert prompt == real.prompt and not prompt.is_relative_to(real.vault.root)
    assert start["cwd"] == str(real.vault.root)
    assert not any(name.startswith(("ANTHROPIC", "OPENAI")) for name in start["env"])


def test_e2e_helper_without_domain_is_read_only(real):
    service = TaskService(real.board, real.coordinator, real.latch, real.vault)
    task, _ = service.add("WRITE_AT wiki/x.md", None)
    assert wait_for(lambda: real.board.get(task.id).status == "done", timeout=20)
    assert not (real.vault.root / "wiki" / "x.md").exists()
    assert real.board.get(task.id).denials == 1
    args = real.starts()[0]["args"]
    assert args[args.index("--tools") + 1] == "Read,Glob,Grep"
    assert not any(line.startswith("[approval") for line in real.lines)  # denied without asking


def test_e2e_two_helpers_in_parallel_third_waits(real):
    service = TaskService(real.board, real.coordinator, real.latch, real.vault)
    tasks = [service.add("SLEEP 3", None)[0] for _ in range(3)]
    assert wait_for(lambda: all(real.board.get(t.id).status == "done" for t in tasks), timeout=40)
    starts = sorted(entry["t"] for entry in real.starts())
    ends = sorted(entry["t"] for entry in real.finishes())
    assert len(starts) == 3 and len(ends) == 3
    # Order, not timing: the first two ran together (both started before any finished),
    # the third started only after one of them had finished.
    assert starts[1] < ends[0]
    assert starts[2] >= ends[0]


def test_e2e_cancel_kills_the_helper_process(real):
    service = TaskService(real.board, real.coordinator, real.latch, real.vault)
    task, _ = service.add("SLEEP 20", None)
    assert wait_for(lambda: real.starts() and real.board.get(task.id).status == "running", timeout=10)
    time.sleep(0.3)
    pid = real.starts()[0]["pid"]
    started = time.monotonic()
    service.cancel_text(task.id)
    assert wait_for(lambda: real.board.get(task.id).status == "cancelled", timeout=10)
    assert time.monotonic() - started < 8
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_e2e_close_kills_a_helper_that_ignores_sigterm(real):
    service = TaskService(real.board, real.coordinator, real.latch, real.vault)
    task, _ = service.add("SLEEP_NOTERM 25", None)
    assert wait_for(lambda: real.starts() and real.board.get(task.id).status == "running", timeout=10)
    time.sleep(0.5)
    pid = real.starts()[0]["pid"]
    real.coordinator.close(join_s=3)
    assert wait_for(lambda: _gone(pid), timeout=10)
    assert real.board.get(task.id).status == "failed"


def _gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_e2e_helper_hook_stops_the_task_and_halts(real):
    service = TaskService(real.board, real.coordinator, real.latch, real.vault)
    task, _ = service.add("HOOK", None)
    assert wait_for(lambda: real.board.get(task.id).status == "failed", timeout=20)
    assert "hook ran" in real.board.get(task.id).error and real.latch.reason
    assert real.broker.scopes() == ["main"]


# --- regressions: close races, lost board updates, vanished records, takeover ---------------------


def test_claim_that_returns_after_close_starts_no_worker(env):
    task = env.board.add("late", None, "chat")
    c = env.coordinator()
    entered, release = threading.Event(), threading.Event()
    original = env.board.claim_next

    def slow_claim():  # e.g. the other chat held the board lock
        entered.set()
        release.wait(5)
        return original()

    env.board.claim_next = slow_claim
    assert c.start()
    assert entered.wait(5)
    closer = threading.Thread(target=c.close, kwargs={"join_s": 5})
    closer.start()
    assert wait_for(lambda: c._stop.is_set())
    release.set()
    closer.join(10)
    assert not closer.is_alive()
    assert env.sessions == {}  # no helper session was made, nothing was sent
    assert env.board.get(task.id).status == "queued"  # back in the queue for the next chat
    assert not c.owner


def test_worker_started_before_close_runs_after_it_does_nothing(env):
    spawned = []
    task = env.board.add("x", None, "chat")
    c = env.coordinator(spawn=lambda fn, name: spawned.append(fn))
    assert c.start(run_thread=False)
    c.tick()
    assert len(spawned) == 1
    c._stop.set()  # the chat closes before the worker thread got going
    spawned[0]()
    record = env.board.get(task.id)
    assert record.status == "failed" and record.error == "interrupted (the chat was closed)"
    assert task.id not in env.sessions


def test_failed_board_update_is_retried(env):
    from personal_agent.tasks import TaskError

    calls = {"n": 0}
    original = env.board.finish

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TaskError("could not save the task board")
        return original(*args, **kwargs)

    env.board.finish = flaky
    task = env.board.add("x", None, "chat")
    c = env.coordinator()
    c.start()
    assert wait_for(lambda: env.board.get(task.id).status == "done")
    assert calls["n"] >= 2 and env.board.get(task.id).result_sha256
    assert c._pending_finish == {}


def test_orphaned_running_record_is_ended(env):
    task = env.board.add("x", None, "chat")
    c = env.coordinator(spawn=lambda fn, name: None)
    assert c.start(run_thread=False)
    assert env.board.claim_next().id == task.id  # running, but no worker of ours runs it
    c.tick()
    record = env.board.get(task.id)
    assert record.status == "failed" and "could not be updated" in record.error
    assert [e.status for e in env.events if e.task_id == task.id] == ["failed"]


def test_cancel_while_the_worker_requeues_ends_cancelled(env):
    ok, blocked = QuotaCheck(True, "within quota"), QuotaCheck(False, "five-hour window at 95 %")
    guard = ScriptedGuard([ok, blocked])  # the claim passes, the worker's re-check does not
    task = env.board.add("x", None, "chat")
    service = TaskService(env.board, None, env.latch, env.vault)

    def make_session(t, scope):
        assert service.cancel_text(t.id) == f"task {t.id}: stopping …"  # just after the claim
        return StubSession(t, {})

    c = Coordinator(env.board, env.broker, guard, env.latch, env.vault, make_session, env.events.append,
                    spawn=lambda fn, name: fn())  # the worker runs inside tick(), deterministically
    try:
        assert c.start(run_thread=False)
        c.tick()
        record = env.board.get(task.id)
        assert record.status == "cancelled"  # not "waiting" with a cancel request nobody acts on
        assert [e.status for e in env.events if e.task_id == task.id] == ["cancelled"]
    finally:
        c.close(join_s=5)


def test_board_set_aside_while_running_keeps_the_helpers_and_tells_the_chat(env):
    env.behaviour["long"] = "block"
    task = env.board.add("long", None, "chat")
    c = env.coordinator()
    c.start()
    assert wait_for(lambda: c.running_count() == 1 and task.id in env.sessions)
    (env.state / "tasks.json").write_text("truncated {")
    assert wait_for(lambda: any(e.status == "notice" for e in env.events))
    notice = notice_line([e for e in env.events if e.status == "notice"][0])
    assert notice.startswith("[tasks: the task board was unreadable and was set aside as tasks.json.corrupt-")
    assert wait_for(lambda: env.board.load()[0] and env.board.get(task.id).status == "running")
    assert not env.sessions[task.id].aborted.is_set()  # never "cancelled"
    env.sessions[task.id].release.set()
    assert wait_for(lambda: env.board.get(task.id).status == "done")
    assert not [e for e in env.events if e.status == "cancelled"]


def test_second_chat_takes_over_when_the_owner_closes(env):
    first = env.coordinator()
    assert first.start()
    other_board = TaskBoard(env.state)
    standby = Coordinator(other_board, env.broker, env.guard, env.latch, env.vault,
                          lambda t, s: StubSession(t, {}), env.events.append, tick_s=0.05)
    service = TaskService(other_board, None, env.latch, env.vault, standby=standby)
    try:
        assert not service.try_take_over()
        first.close(join_s=5)
        reply, is_error = service.command(type("C", (), {"kind": "task_add", "arg": "after", "domain": None})())
        assert not is_error and reply.startswith("[tasks: this chat now runs the task board]\n")
        assert "other open chat" not in reply
        assert service.coordinator is standby and standby.owner
        task_id = reply.split("\n")[1].split()[1]
        assert wait_for(lambda: other_board.get(task_id).status == "done")
        assert not service.try_take_over()  # only once
    finally:
        standby.close(join_s=5)
