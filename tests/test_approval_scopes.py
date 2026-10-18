import json
import stat
import threading
import time
from types import SimpleNamespace

import pytest

from personal_agent import approval
from personal_agent.approval import (
    CONTEXT_HINT,
    HELPER_OUTPUT_CONTEXT,
    MAX_PROMPTS_PER_HELPER_TASK,
    ApprovalBroker,
    ApprovalRequest,
    ConsoleChannel,
    Level,
)
from personal_agent.approval_server import ask_broker
from personal_agent.remote import RemoteChannel

TASK = "abc123"
DOMAIN_FILE = "wiki/domains/research.md"


class Channel:
    """Scripted channel: records every request with start/end times; answer may be callable."""

    def __init__(self, answer=True):
        self.answer = answer
        self.calls = []
        self.lock = threading.Lock()

    def ask(self, request, deadline):
        start = time.monotonic()
        try:
            return self.answer(request, deadline) if callable(self.answer) else self.answer
        finally:
            with self.lock:
                self.calls.append(SimpleNamespace(request=request, start=start, end=time.monotonic(),
                                                  deadline=deadline))


@pytest.fixture
def broker(tmp_path):
    work = tmp_path / "work"
    (work / "wiki" / "domains").mkdir(parents=True)
    (work / "wiki" / "domains" / "research.md").write_text("# Domain: research\n\nLinks:\n\n## Notes\n")
    b = ApprovalBroker(work, channel=Channel(), bind_wait_s=0.3)
    b.start(tmp_path / "run")
    b.begin_turn()
    yield b
    b.close()


def helper(broker, task_id=TASK, domain="research", title="Summarise the backups", delegated=False):
    scope = broker.open_scope(f"task-{task_id}", kind="helper", task_id=task_id, title=title,
                              domain=domain, delegated=delegated)
    scope.begin_turn()
    return scope


def announce(scope_or_broker, name, tool_input, tool_use_id=None):
    tool_use_id = tool_use_id or f"toolu_{time.monotonic_ns()}"
    scope_or_broker.observe({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tool_use_id, "name": name, "input": tool_input}]}})
    return tool_use_id


def ask(broker, scope, name, tool_input):
    tool_use_id = announce(scope if scope is not None else broker, name, tool_input)
    return broker.decide(name, tool_input, tool_use_id, scope=scope)


def append(note=f"- 2026-10-18 (task {TASK}): restic supports append-only repositories", path=DOMAIN_FILE,
           old="## Notes\n"):
    return {"file_path": path, "old_string": old, "new_string": old + note + "\n"}


# --- scopes ---------------------------------------------------------------------------


def test_scope_files_tokens_and_close(broker):
    scope = helper(broker)
    path = scope.mcp_config_path
    assert path.name == f"mcp-task-{TASK}.json" and stat.S_IMODE(path.stat().st_mode) == 0o600
    env = json.loads(path.read_text())["mcpServers"]["pa"]["env"]
    assert env["PA_APPROVAL_TOKEN"] == scope.token != broker.token
    assert "PA_APPROVAL_DELEGATE" not in env
    assert broker.scopes() == ["main", f"task-{TASK}"]
    socket_path = str(broker.socket_path)
    verdict = ask_broker(socket_path, scope.token, "Read", {"file_path": "a"}, "toolu_x", 5)
    assert verdict["message"] == "no matching tool call"  # the token is accepted
    broker.close_scope(scope)
    broker.close_scope(scope)  # idempotent
    assert not path.exists() and broker.scopes() == ["main"]
    verdict = ask_broker(socket_path, scope.token, "Read", {"file_path": "a"}, "toolu_x", 5)
    assert verdict["message"] == "unauthorized"


def test_scope_limits_and_validation(broker):
    with pytest.raises(RuntimeError):
        broker.open_scope("Bad Name", task_id=TASK)
    with pytest.raises(RuntimeError):
        broker.open_scope("task-x", task_id="nothex")
    with pytest.raises(RuntimeError):
        broker.open_scope("task-y", task_id=TASK, domain="Bad")
    for i in range(3):
        broker.open_scope(f"task-{i}0000{i}", task_id=f"{i}0000{i}")
    with pytest.raises(RuntimeError, match="too many"):
        broker.open_scope("task-ffffff", task_id="ffffff")
    with pytest.raises(RuntimeError, match="already open"):
        broker.open_scope("task-000000", task_id="000000")


def test_unstarted_broker_cannot_open_scopes(tmp_path):
    with pytest.raises(RuntimeError, match="not running"):
        ApprovalBroker(tmp_path).open_scope("task-abc123", task_id=TASK)


def test_main_api_acts_on_the_main_scope(broker):
    assert broker.main.name == "main" and broker.turn_active is broker.main.turn_active
    assert ask(broker, None, "Write", {"file_path": "notes.md", "content": "x"}).allow
    assert broker.channel.calls[-1].request.scope == "main"
    assert broker.channel.calls[-1].request.scope_label == "chat"


def test_binding_is_isolated_between_scopes(broker):
    scope = helper(broker)
    data = append()
    in_helper = announce(scope, "Edit", data)
    assert broker.decide("Edit", data, in_helper).message == "no matching tool call"  # main cannot approve it
    in_main = announce(broker, "Write", {"file_path": "n.md", "content": "x"})
    assert broker.decide("Write", {"file_path": "n.md", "content": "x"}, in_main, scope=scope).message \
        == "no matching tool call"
    # the same over the socket, with each scope's own token
    socket_path = str(broker.socket_path)
    assert ask_broker(socket_path, scope.token, "Write", {"file_path": "n.md", "content": "x"}, in_main, 5)[
        "behavior"] == "deny"
    assert ask_broker(socket_path, broker.token, "Edit", data, in_helper, 5)["behavior"] == "deny"
    assert broker.channel.calls == []
    assert ask_broker(socket_path, scope.token, "Edit", data, in_helper, 5)["behavior"] == "allow"


# --- the prompt queue --------------------------------------------------------------------


def test_one_prompt_at_a_time_main_first(broker):
    gate = threading.Event()
    entered = threading.Event()

    def answer(request, deadline):
        if not entered.is_set():
            entered.set()
            gate.wait(5)
        return True

    broker.channel = Channel(answer)
    first = helper(broker, "aaaaaa")
    second = helper(broker, "bbbbbb")
    results = {}

    def run(name, scope, tool, data):
        results[name] = ask(broker, scope, tool, data)

    threads = [threading.Thread(target=run, args=("first", first, "Edit",
                                                  append(f"- 2026-10-18 (task aaaaaa): a")))]
    threads[0].start()
    assert entered.wait(5)
    threads.append(threading.Thread(target=run, args=("second", second, "Edit",
                                                      append(f"- 2026-10-18 (task bbbbbb): b"))))
    threads[1].start()
    time.sleep(0.3)  # the helper queues before the main request
    threads.append(threading.Thread(target=run, args=("main", None, "Write", {"file_path": "m.md", "content": "x"})))
    threads[2].start()
    time.sleep(0.3)
    gate.set()
    for t in threads:
        t.join(10)
    calls = broker.channel.calls
    assert [c.request.scope for c in calls] == ["task-aaaaaa", "main", "task-bbbbbb"]
    for earlier, later in zip(calls, calls[1:]):
        assert earlier.end <= later.start  # strictly one at a time
    assert all(v.allow for v in results.values())


def test_waiting_caps_per_scope_and_total(broker, monkeypatch):
    gate = threading.Event()
    entered = threading.Event()

    def answer(request, deadline):
        entered.set()
        gate.wait(10)
        return True

    broker.channel = Channel(answer)
    scopes = [helper(broker, f"{i}{i}{i}{i}{i}{i}", domain=None) for i in range(3)]
    for scope in scopes:
        scope.domain_file = DOMAIN_FILE  # let them ask for appends without a domain folder each
    threads = []

    def run(scope, tool, data, out):
        out.append(ask(broker, scope, tool, data))

    results = []
    holder = threading.Thread(target=run, args=(None, "Write", {"file_path": "h.md", "content": "x"}, results))
    holder.start()
    assert entered.wait(5)
    for i in range(3):
        threads.append(threading.Thread(target=run, args=(None, "Write", {"file_path": f"m{i}.md", "content": "x"},
                                                          results)))
    for scope in scopes[:1]:
        for i in range(3):
            note = f"- 2026-10-18 (task {scope.task_id}): note {i}"
            threads.append(threading.Thread(target=run, args=(scope, "Edit", append(note), results)))
    for t in threads:
        t.start()
    for _ in range(100):
        if broker._waiting == 6:
            break
        time.sleep(0.02)
    assert broker._waiting == 6
    over_main = ask(broker, None, "Write", {"file_path": "x.md", "content": "x"})
    assert over_main.message == "too many pending approvals"  # per scope (3) and total (6)
    note = f"- 2026-10-18 (task {scopes[1].task_id}): late"
    over_total = ask(broker, scopes[1], "Edit", append(note))
    assert over_total.message == "too many pending approvals"  # total
    gate.set()
    for t in [holder, *threads]:
        t.join(15)


def test_refusal_cap_is_per_scope(broker):
    broker.channel = Channel(lambda r, d: r.scope == "main")
    scope = helper(broker)
    for i in range(2):
        assert not ask(broker, scope, "Edit", append(f"- 2026-10-18 (task {TASK}): n{i}")).allow
    third = ask(broker, scope, "Edit", append(f"- 2026-10-18 (task {TASK}): n3"))
    assert third.message == "too many refused approvals in this turn"
    assert ask(broker, None, "Write", {"file_path": "ok.md", "content": "x"}).allow


def test_helper_prompt_budget_and_deadline(broker):
    scope = helper(broker)
    for i in range(MAX_PROMPTS_PER_HELPER_TASK):
        assert ask(broker, scope, "Edit", append(f"- 2026-10-18 (task {TASK}): n{i}")).allow
    scope.begin_turn()  # the budget is per task, not per turn
    fourth = ask(broker, scope, "Edit", append(f"- 2026-10-18 (task {TASK}): n4"))
    assert fourth.message == "helper used its approval budget"
    assert len(broker.channel.calls) == MAX_PROMPTS_PER_HELPER_TASK
    call = broker.channel.calls[0]
    assert call.deadline - call.start <= approval.HELPER_PROMPT_TIMEOUT_S + 1  # not the 300 s main timeout


def test_main_preempts_a_pending_helper_prompt(broker):
    shown = threading.Event()

    def answer(request, deadline):
        if request.scope == "main":
            return True
        shown.set()
        while not broker.request_stopped(request):
            time.sleep(0.02)
        return False

    broker.channel = Channel(answer)
    scope = helper(broker)
    results = {}
    t = threading.Thread(target=lambda: results.update(helper=ask(
        broker, scope, "Edit", append())))
    t.start()
    assert shown.wait(5)
    started = time.monotonic()
    main = ask(broker, None, "Write", {"file_path": "m.md", "content": "x"})
    t.join(5)
    assert main.allow and time.monotonic() - started < 3
    assert not results["helper"].allow and "withdrawn" in results["helper"].message
    assert scope._refusals == 0  # withdrawn, not refused
    assert [c.request.scope for c in broker.channel.calls] == [f"task-{TASK}", "main"]


def test_end_turn_ends_a_pending_console_prompt(broker, monkeypatch):
    monkeypatch.setattr(approval, "stdin_is_tty", lambda: True)
    monkeypatch.setattr(approval, "discard_typeahead", lambda: True)
    lines = []

    def never(timeout):
        time.sleep(timeout)
        return None

    broker.channel = ConsoleChannel(threading.Lock(), read_line=never, write=lines.append,
                                    stop=broker.closing.is_set, stop_for=broker.request_stopped)
    scope = helper(broker)
    result = {}
    t = threading.Thread(target=lambda: result.update(v=ask(broker, scope, "Edit", append())))
    t.start()
    for _ in range(100):
        if any(line.startswith("[approval ·") for line in lines):
            break
        time.sleep(0.02)
    started = time.monotonic()
    scope.end_turn()  # e.g. the task was cancelled
    t.join(5)
    assert not result["v"].allow and time.monotonic() - started < 2


def test_main_idle_helper_prompt_is_approved_on_the_console(broker, monkeypatch):
    monkeypatch.setattr(approval, "stdin_is_tty", lambda: True)
    monkeypatch.setattr(approval, "discard_typeahead", lambda: True)
    broker.end_turn()  # the person sits idle at you>
    lines = []
    broker.channel = ConsoleChannel(threading.Lock(), read_line=lambda timeout: "y", write=lines.append,
                                    stop=broker.closing.is_set, stop_for=broker.request_stopped)
    scope = helper(broker)
    assert ask(broker, scope, "Edit", append()).allow
    assert lines[0] == f"[approval · task {TASK} (research)] level 2 (write): Edit"
    assert lines[1] == '  task: "Summarise the backups"'


# --- what helpers may write -------------------------------------------------------------


@pytest.mark.parametrize("tool, data, message", [
    ("Write", {"file_path": "outputs/x.md", "content": "x"}, "may not write outputs/"),
    ("Write", {"file_path": "outputs/tasks/abc123.md", "content": "x"}, "may not write outputs/"),
    ("Write", {"file_path": "wiki/x.md", "content": "x"}, "only append notes"),
    ("Write", {"file_path": "heute.md", "content": "x"}, "only append notes"),
    ("Write", {"file_path": "raw/inbox-2026-10-18.md", "content": "x"}, "only append notes"),
    ("Write", {"file_path": "CLAUDE.md", "content": "x"}, "only append notes"),
    ("Write", {"file_path": DOMAIN_FILE, "content": "x"}, "only append notes"),
    ("Edit", append(path="wiki/domains/finance.md"), "only append notes"),
    ("Edit", append(path="wiki/domains/sub/research.md"), "only append notes"),
    ("Edit", append(path="Wiki/Domains/Research.md"), "only append notes"),
    ("NotebookEdit", {"notebook_path": DOMAIN_FILE, "new_source": "x"}, "only append notes"),
])
def test_helper_writes_outside_its_domain_notes_are_denied_without_asking(broker, tool, data, message):
    scope = helper(broker)
    verdict = ask(broker, scope, tool, data)
    assert not verdict.allow and message in verdict.message
    assert broker.channel.calls == []


@pytest.mark.parametrize("data, message", [
    ({"file_path": DOMAIN_FILE, "old_string": "Links:\n", "new_string": "Links: evil\n"}, "append-only"),
    (append("- [ ] buy a new disk"), "must look like"),
    (append("- 2026-10-18 (task ffffff): someone else's id"), "must look like"),
    (append("\n".join(f"- 2026-10-18 (task {TASK}): n{i}" for i in range(4))), "at most 3 lines"),
    (append(f"- 2026-10-18 (task {TASK}): x" + "y" * 600), "at most 600"),
    (append(f"- 2026-10-18 (task {TASK}): hidden​"), "invisible"),
    (append("# New heading"), "must look like"),
    (append("Links: finance"), "must look like"),
    (dict(append(), replace_all=True), "append-only"),
    ({"file_path": DOMAIN_FILE, "old_string": "## Notes\n", "new_string": "## Old\n"}, "append-only"),
    ({"file_path": DOMAIN_FILE, "old_string": "## Notes", "new_string": f"## Notes- 2026-10-18 (task {TASK}): x"},
     "new line"),
])
def test_domain_appends_are_format_checked(broker, data, message):
    scope = helper(broker)
    verdict = ask(broker, scope, "Edit", data)
    assert not verdict.allow and message in verdict.message, verdict.message
    assert broker.channel.calls == []


def test_valid_domain_append_is_prompted_with_the_full_text(broker):
    scope = helper(broker)
    note = f"- 2026-10-18 (task {TASK}): " + "z" * 250
    assert ask(broker, scope, "Edit", append(note)).allow
    request = broker.channel.calls[0].request
    assert request.level == Level.WRITE and request.full is None and "…" not in request.summary
    assert json.dumps(note + "\n") in request.summary
    assert request.context == "helper task" or request.context is None


def test_helper_without_domain_and_helper_skill_are_denied(broker):
    scope = helper(broker, domain=None)
    assert not ask(broker, scope, "Edit", append()).allow
    assert ask(broker, scope, "Skill", {"skill": "core:daily-plan"}).message == "this skill is not allowed"
    broker.allowed_skills = frozenset({"core:daily-plan"})
    assert ask(broker, scope, "Skill", {"skill": "core:daily-plan"}).message == "this skill is not allowed"
    assert ask(broker, scope, "Read", {"file_path": "wiki/x.md"}).allow  # reading is fine
    assert broker.channel.calls == []


# --- the main session and helper data ------------------------------------------------------


def test_main_never_writes_task_results(broker):
    for path in ("outputs/tasks/abc123.md", "outputs/tasks/new.md", "OUTPUTS/tasks/x.md"):
        verdict = ask(broker, None, "Write", {"file_path": path, "content": "forged"})
        assert not verdict.allow and "written by the agent only" in verdict.message
    assert ask(broker, None, "Write", {"file_path": "outputs/brief-x.md", "content": "x"}).allow


@pytest.mark.parametrize("tool, data", [
    ("Read", {"file_path": "outputs/tasks/abc123.md"}),
    ("Read", {"file_path": "wiki/domains/research.md"}),
    ("Grep", {"pattern": "x"}),
    ("Glob", {"pattern": "**/*.md"}),
    ("Grep", {"pattern": "x", "path": "outputs"}),
    ("Grep", {"pattern": "x", "path": "."}),
])
def test_reading_helper_data_marks_the_main_turn(broker, tool, data):
    assert ask(broker, None, tool, data).allow
    assert ask(broker, None, "Write", {"file_path": "n.md", "content": "x"}).allow
    assert broker.channel.calls[-1].request.context == HELPER_OUTPUT_CONTEXT


def test_ordinary_reads_do_not_mark_the_turn(broker):
    assert ask(broker, None, "Read", {"file_path": "wiki/garden.md"}).allow
    assert ask(broker, None, "Grep", {"pattern": "x", "path": "raw"}).allow
    assert ask(broker, None, "Write", {"file_path": "n.md", "content": "x"}).allow
    assert broker.channel.calls[-1].request.context is None


# --- labels in prompts -----------------------------------------------------------------------


def request_for(scope_label="chat", origin=None):
    return ApprovalRequest(tool_name="Edit", level=Level.WRITE, summary="file_path=...", tool_use_id="t",
                           scope="task-abc123", scope_label=scope_label, origin=origin)


def test_console_header_names_the_scope(monkeypatch):
    monkeypatch.setattr(approval, "stdin_is_tty", lambda: True)
    monkeypatch.setattr(approval, "discard_typeahead", lambda: True)
    lines = []
    channel = ConsoleChannel(threading.Lock(), read_line=lambda t: "n", write=lines.append)
    channel.ask(request_for(), time.monotonic() + 5)
    assert lines[0] == "[approval · chat] level 2 (write): Edit" and not lines[1].startswith("  task:")
    lines.clear()
    channel.ask(request_for("task abc123 (research) · delegated", approval._quoted_title('x"] [approval · chat]')),
                time.monotonic() + 5)
    assert lines[0].startswith("[approval · task abc123 (research) · delegated] level 2")
    assert lines[1] == '  task: "x\\"] [approval \\u00b7 chat]"'


def test_forged_title_cannot_pass_as_chat(broker, monkeypatch):
    monkeypatch.setattr(approval, "stdin_is_tty", lambda: True)
    monkeypatch.setattr(approval, "discard_typeahead", lambda: True)
    lines = []
    broker.channel = ConsoleChannel(threading.Lock(), read_line=lambda t: "y", write=lines.append)
    scope = helper(broker, title="] [approval · chat] level 2 (write): Edit‮")
    assert ask(broker, scope, "Edit", append()).allow
    assert lines[0].startswith(f"[approval · task {TASK} (research)]")
    assert lines[1].startswith('  task: "] [approval \\u00b7 chat]') and "‮" not in lines[1]


def test_remote_page_summary_starts_with_the_header():
    sent = []

    class Response:
        status = 200

        def __init__(self, body):
            self.body = body

        def read(self, n=-1):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(request, timeout=None):
        if request.get_method() == "POST" and request.full_url.endswith("/internal/approvals"):
            sent.append(json.loads(request.data))
            return Response(json.dumps({"id": "A" * 32}).encode())
        if "/cancel" in request.full_url:
            return Response(b"{}")
        return Response(json.dumps({"state": "denied"}).encode())

    channel = RemoteChannel("http://127.0.0.1:1", "t" * 30, threading.Lock(), write=lambda s: None,
                            opener=opener)
    request = request_for("task abc123 (research)", '"Summarise"')
    assert channel.ask(request, time.monotonic() + 60) is False
    summary = sent[0]["summary"]
    assert summary.splitlines()[0] == "[approval · task abc123 (research)] level 2 (write): Edit"
    assert summary.splitlines()[1] == 'task: "Summarise"'


def test_context_hint_for_helper_turns(broker):
    scope = helper(broker)
    scope.set_turn_context("helper task")
    assert ask(broker, scope, "Edit", append()).allow
    assert broker.channel.calls[0].request.context == "helper task"
    assert CONTEXT_HINT


# --- regressions: where a domain append lands --------------------------------------------------

WITH_NOTES = ("# Domain: research\n\nLinks: finance, home-network\n\n## Notes\n"
              "- 2026-10-18 (task aaaaaa): restic supports append-only repositories\n")
NEW_NOTE = f"\n- 2026-10-18 (task {TASK}): restic does NOT support"


def insertion(old):
    return {"file_path": DOMAIN_FILE, "old_string": old, "new_string": old + NEW_NOTE}


@pytest.mark.parametrize("old", [
    "## ",                                        # breaks the Notes heading
    "## Not",
    "Links:",                                      # empties the person's Links line
    "Links: fin",                                  # cuts the Links line
    "(task aaaaaa): restic supports",              # splits an existing note mid-line
    "## Notes\n",                                  # mid-file once notes exist
    "# Domain: research\n",
])
def test_domain_append_in_the_middle_of_the_file_is_denied_without_asking(broker, old):
    (broker.workdir / DOMAIN_FILE).write_text(WITH_NOTES)
    scope = helper(broker)
    verdict = ask(broker, scope, "Edit", insertion(old))
    assert not verdict.allow and "appended" in verdict.message, verdict.message
    assert broker.channel.calls == []
    assert (broker.workdir / DOMAIN_FILE).read_text() == WITH_NOTES


def test_domain_append_at_the_end_of_the_notes_is_prompted(broker):
    (broker.workdir / DOMAIN_FILE).write_text(WITH_NOTES)
    scope = helper(broker)
    old = "append-only repositories\n"
    note = f"- 2026-10-18 (task {TASK}): borg too"
    assert ask(broker, scope, "Edit", {"file_path": DOMAIN_FILE, "old_string": old,
                                       "new_string": old + note + "\n"}).allow
    assert json.dumps(note + "\n") in broker.channel.calls[0].request.summary


def test_domain_append_needs_a_readable_file_with_a_notes_heading(broker, tmp_path):
    path = broker.workdir / DOMAIN_FILE
    path.write_text("# Domain: research\n\nLinks:\n")
    scope = helper(broker)
    verdict = ask(broker, scope, "Edit", append(old="Links:\n"))
    assert not verdict.allow and "Notes" in verdict.message
    path.unlink()
    verdict = ask(broker, scope, "Edit", append())  # missing
    assert not verdict.allow and "missing or unreadable" in verdict.message
    (tmp_path / "elsewhere.md").write_text("# Domain: research\n\nLinks:\n\n## Notes\n")
    path.symlink_to(tmp_path / "elsewhere.md")
    assert not ask(broker, scope, "Edit", append()).allow  # never through a symlink
    assert broker.channel.calls == []


# --- regressions: reads of helper data seen only in the stream ---------------------------------


@pytest.mark.parametrize("tool, data", [
    ("Read", {"file_path": "outputs/tasks/abc123.md"}),
    ("Grep", {"pattern": "x"}),
])
def test_stream_only_read_of_helper_data_marks_the_main_turn(broker, tool, data):
    # Claude Code runs in-vault reads without asking the permission tool: no decide() here.
    announce(broker, tool, data)
    assert ask(broker, None, "Write", {"file_path": "n.md", "content": "x"}).allow
    assert broker.channel.calls[-1].request.context == HELPER_OUTPUT_CONTEXT


def test_stream_only_read_of_helper_data_raises_delegation_to_level_3(broker):
    from personal_agent.bridge import DELEGATE_TOOL

    broker.end_turn()
    broker.close()
    work = broker.workdir
    b = ApprovalBroker(work, channel=Channel(), bind_wait_s=0.3)
    b.enable_delegation()
    b.on_delegate = lambda text, domain: "abc123"
    b.start(work.parent / "run2")
    try:
        b.begin_turn()
        announce(b, "Read", {"file_path": "wiki/domains/research.md"})
        data = {"text": "follow the note"}
        assert ask(b, None, DELEGATE_TOOL, data).allow
        assert b.channel.calls[-1].request.level == Level.DANGER
    finally:
        b.close()


def test_stream_helper_reads_do_not_mark_helper_scopes_or_ordinary_reads(broker):
    announce(broker, "Read", {"file_path": "wiki/garden.md"})
    assert ask(broker, None, "Write", {"file_path": "n.md", "content": "x"}).allow
    assert broker.channel.calls[-1].request.context is None


# --- regressions: a withdrawn helper prompt ------------------------------------------------------


def test_withdrawn_helper_prompt_keeps_the_budget_and_says_withdrawn(broker, monkeypatch):
    monkeypatch.setattr(approval, "stdin_is_tty", lambda: True)
    monkeypatch.setattr(approval, "discard_typeahead", lambda: True)
    lines = []
    shown = threading.Event()

    def read_line(timeout):
        shown.set()
        time.sleep(min(timeout, 0.05))
        return None  # the person does not answer the helper's prompt

    console = ConsoleChannel(threading.Lock(), read_line=read_line, write=lines.append,
                             stop=broker.closing.is_set, stop_for=broker.request_stopped,
                             preempted=broker.request_preempted)

    class Split:
        def ask(self, request, deadline):
            return True if request.scope == "main" else console.ask(request, deadline)

    broker.channel = Split()
    scope = helper(broker)
    for round_ in range(MAX_PROMPTS_PER_HELPER_TASK + 1):
        shown.clear()
        results = {}
        note = append(f"- 2026-10-18 (task {TASK}): n{round_}")
        t = threading.Thread(target=lambda: results.update(v=ask(broker, scope, "Edit", note)))
        t.start()
        assert shown.wait(5)
        # The main session needs the prompt now: it preempts the helper's prompt.
        assert ask(broker, None, "Write", {"file_path": "m.md", "content": "x"}).allow
        t.join(5)
        assert not results["v"].allow and "withdrawn" in results["v"].message
    assert scope._prompts_shown == 0 and scope._refusals == 0  # no budget used by withdrawn prompts
    assert lines.count("[withdrawn: the chat needs the prompt]") == MAX_PROMPTS_PER_HELPER_TASK + 1
    assert "[denied]" not in lines


# --- regressions: delegation without a task service ------------------------------------------------


def test_delegation_without_a_task_service_is_denied_without_asking(tmp_path):
    from personal_agent.bridge import DELEGATE_TOOL

    work = tmp_path / "work"
    work.mkdir()
    b = ApprovalBroker(work, channel=Channel(), bind_wait_s=0.3)
    b.enable_delegation()  # but on_delegate stays None: the task service did not start
    b.start(tmp_path / "run")
    try:
        b.begin_turn()
        verdict = ask(b, None, DELEGATE_TOOL, {"text": "background work"})
        assert not verdict.allow and verdict.message == "delegation is off"
        assert b.channel.calls == []
    finally:
        b.close()


def test_enable_delegation_after_start_rewrites_the_main_config(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    b = ApprovalBroker(work, channel=Channel())
    path = b.start(tmp_path / "run")
    try:
        assert "PA_APPROVAL_DELEGATE" not in json.loads(path.read_text())["mcpServers"]["pa"]["env"]
        b.enable_delegation()
        env = json.loads(path.read_text())["mcpServers"]["pa"]["env"]
        assert env["PA_APPROVAL_DELEGATE"] == "1" and env["PA_APPROVAL_TOKEN"] == b.token
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert sorted(p.name for p in path.parent.iterdir()) == ["mcp.json", "s"]
    finally:
        b.close()
