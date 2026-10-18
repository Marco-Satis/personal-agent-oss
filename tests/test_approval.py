import json
import os
import secrets
import shlex
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from personal_agent import approval, approval_server
from personal_agent.approval import (
    PERMISSION_TOOL,
    ApprovalBroker,
    ApprovalRequest,
    ConsoleChannel,
    Level,
    classify,
    render_input,
    tokens,
)
from personal_agent.approval_server import ask_broker, handle, input_digest, shrink_input
from personal_agent.config import Config, ConfigError

FAKE = Path(__file__).resolve().parent / "fake_claude.py"
SERVER_FILE = str(Path(approval_server.__file__).resolve())


# --- helpers -------------------------------------------------------------------


class FakeChannel:
    """Records requests; answers with `answer` (a bool or a callable)."""

    def __init__(self, answer=True, delay=0.0):
        self.answer = answer
        self.delay = delay
        self.calls = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def ask(self, request, deadline):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append(request)
        try:
            if self.delay:
                time.sleep(self.delay)
            return self.answer(request) if callable(self.answer) else self.answer
        finally:
            with self._lock:
                self.active -= 1


def make_broker(tmp_path, channel=None, **kw):
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    kw.setdefault("bind_wait_s", 0.2)
    broker = ApprovalBroker(workdir, channel=channel, **kw)
    broker.begin_turn()
    return broker


def announce(broker, name, tool_input, tool_use_id=None):
    tool_use_id = tool_use_id or f"toolu_{secrets.token_hex(6)}"
    broker.observe({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tool_use_id, "name": name, "input": tool_input}]}})
    return tool_use_id


def ask(broker, name, tool_input):
    return broker.decide(name, tool_input, announce(broker, name, tool_input))


def reader(answers):
    it = iter(answers)

    def read(_timeout):
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    return read


@pytest.fixture
def tty(monkeypatch):
    monkeypatch.setattr(approval, "stdin_is_tty", lambda: True)
    monkeypatch.setattr(approval, "discard_typeahead", lambda: True)


# --- classification ------------------------------------------------------------


@pytest.mark.parametrize(
    "name, tool_input, level",
    [
        ("Read", {"file_path": "notes/a.md"}, 1),
        ("Glob", {"pattern": "**/*.md"}, 1),
        ("Grep", {"pattern": "todo"}, 1),
        ("Write", {"file_path": "notes/a.md", "content": "x"}, 2),
        ("Edit", {"file_path": "notes/a.md", "old_string": "a", "new_string": "b"}, 2),
        ("NotebookEdit", {"notebook_path": "n.ipynb", "new_source": "x"}, 2),
        ("Write", {"file_path": ".claude/settings.json", "content": "{}"}, 3),
        ("Write", {"file_path": "CLAUDE.md", "content": "x"}, 3),
        ("Write", {"file_path": "claude.MD", "content": "x"}, 3),
        ("Write", {"file_path": "AGENTS.md", "content": "x"}, 3),
        ("Write", {"file_path": "x/.mcp.json", "content": "{}"}, 3),
        ("Write", {"file_path": ".git/config", "content": "x"}, 3),
        ("Write", {"file_path": ".github/workflows/ci.yml", "content": "x"}, 3),
        ("Edit", {"file_path": ".envrc", "old_string": "a", "new_string": "b"}, 3),
        # MCP tools: names are chosen by the server, so read-looking names are not auto-allowed
        ("mcp__mail__list_messages", {}, 2),
        ("mcp__cal__get_event", {"id": "1"}, 2),
        ("mcp__db__query", {"sql": "select 1"}, 2),
        ("mcp__web__get_url", {"url": "https://x"}, 2),
        ("mcp__web__search", {"q": "x"}, 2),
        ("mcp__x__search_and_replace", {}, 2),
        ("mcp__notes__create_draft", {}, 2),
        ("mcp__mail__send_message", {}, 3),
        ("mcp__drive__delete_file", {}, 3),
        ("mcp__shop__buyItem", {}, 3),
        ("mcp__x__list_and_delete", {}, 3),
        ("mcp__payments__get_balance", {}, 3),
        ("mcp__x__get_and_reply", {}, 3),
        ("mcp__x__find_and_modify", {}, 2),
        ("mcp__x__frobnicate", {}, 3),
        ("mcp__x__message_user", {}, 3),
        ("mcp__drive__set_permissions", {}, 3),
        ("mcp__team__add_member", {}, 3),
        ("mcp__drive__copy_file", {}, 3),
        ("mcp__drive__move_file", {}, 3),
        ("mcp__drive__upload_file", {}, 3),
        ("mcp__repo__push_branch", {}, 3),
        ("mcp__cal__create_event", {"title": "x", "attendees": ["a@example.com"]}, 3),
        ("mcp__cal__create_event", {"title": "x"}, 2),
        ("mcp__x", {}, 3),
        ("mcp__x__get_ѕend", {}, 3),  # Cyrillic look-alike
        ("Reаd", {"file_path": "a"}, 3),
        ("Bash", {"command": "ls"}, 3),
        ("WebFetch", {"url": "https://x"}, 3),
        ("Task", {}, 3),
        ("", {}, 3),
        ("Read", "not a dict", 3),
        (None, {}, 3),
    ],
)
def test_classify(name, tool_input, level):
    assert classify(name, tool_input) == level


def test_sensitive_parts_are_relative_to_workdir(tmp_path):
    workdir = tmp_path / ".claude" / "work"
    workdir.mkdir(parents=True)
    assert classify("Write", {"file_path": "notes/a.md"}, workdir) == Level.WRITE
    assert classify("Write", {"file_path": str(workdir / "notes/a.md")}, workdir) == Level.WRITE
    assert classify("Write", {"file_path": str(workdir / ".git" / "config")}, workdir) == Level.DANGER


def test_tokens():
    assert tokens("mcp__shop__buyItem") == ["mcp", "shop", "buy", "item"]
    assert tokens("NotebookEdit") == ["notebook", "edit"]
    assert tokens("get-HTTPServer.status") == ["get", "http", "server", "status"]


# --- rendering -----------------------------------------------------------------


def test_render_escapes_control_and_bidi_characters():
    text = render_input("Write", {"file_path": "a.md", "content": "\x1b[31mred‮\nline"})
    assert "\x1b" not in text and "‮" not in text and "\n" not in text
    assert "\\u001b" in text and "\\u202e" in text
    assert 'file_path="a.md"' in text and "content=14 chars" in text


def test_render_marks_every_cut():
    text = render_input("Bash", {"command": "x" * 1000}, limit=100)
    assert "…(+" in text and len(text) < 130
    many = render_input("mcp__x__create", {f"k{i}": i for i in range(10)})
    assert "(+4 more keys)" in many
    long_write = render_input("Write", {"file_path": "a", "content": "y" * 500})
    assert "content=500 chars" in long_write and "…(+340 chars)" in long_write


def test_render_edit():
    text = render_input("Edit", {"file_path": "a.md", "old_string": "old text", "new_string": "new text"})
    assert text == 'file_path="a.md", old="old text", new="new text"'


# --- broker decisions ----------------------------------------------------------


def test_level1_allows_without_channel(tmp_path):
    channel = FakeChannel()
    broker = make_broker(tmp_path, channel)
    verdict = ask(broker, "Read", {"file_path": "notes/a.md"})
    assert verdict.allow and channel.calls == []
    assert ask(broker, "Glob", {"pattern": "**/*.md"}).allow


@pytest.mark.parametrize(
    "name, tool_input",
    [
        ("Read", {"file_path": "/etc/passwd"}),
        ("Read", {"file_path": "../x"}),
        ("Read", {"file_path": "~/.ssh/id_rsa"}),
        ("Glob", {"pattern": "/home/**"}),
        ("Glob", {"pattern": "*.md", "path": "/"}),
        ("Grep", {"pattern": "key", "path": "/etc"}),
        ("Grep", {"pattern": "key", "glob": "../../**"}),
        ("Write", {"file_path": "/tmp/x", "content": "x"}),
        ("Read", {"file_path": 7}),
    ],
)
def test_paths_outside_workdir_are_denied_without_asking(tmp_path, name, tool_input):
    channel = FakeChannel()
    broker = make_broker(tmp_path, channel)
    verdict = ask(broker, name, tool_input)
    assert not verdict.allow and channel.calls == []


def test_symlink_out_of_workdir_is_denied(tmp_path):
    channel = FakeChannel()
    broker = make_broker(tmp_path, channel)
    (tmp_path / "outside").mkdir()
    (broker.workdir / "link").symlink_to(tmp_path / "outside")
    assert not ask(broker, "Read", {"file_path": "link/secret"}).allow
    assert channel.calls == []


def test_level2_follows_the_answer(tmp_path):
    channel = FakeChannel(answer=True)
    broker = make_broker(tmp_path, channel)
    verdict = ask(broker, "Write", {"file_path": "a.md", "content": "x"})
    assert verdict.allow and channel.calls[0].level == Level.WRITE
    channel.answer = False
    assert not ask(broker, "Write", {"file_path": "a.md", "content": "x"}).allow


def test_protected_paths_are_hard_denied(tmp_path):
    channel = FakeChannel()
    broker = make_broker(tmp_path, channel)
    for path in (".git/hooks/pre-commit", ".claude/settings.json", ".mcp.json", ".vscode/tasks.json", ".envrc"):
        verdict = ask(broker, "Write", {"file_path": path, "content": "x"})
        assert not verdict.allow and "protected" in verdict.message
    assert channel.calls == []
    assert ask(broker, "Write", {"file_path": "CLAUDE.md", "content": "x"}).allow
    assert channel.calls[0].level == Level.DANGER


def test_level3_shows_complete_input_when_summary_is_cut(tmp_path):
    channel = FakeChannel()
    broker = make_broker(tmp_path, channel)
    hidden = "a@example.com" + " " * 200 + "attacker@example.net"
    ask(broker, "Bash", {"command": "echo", "to": hidden})
    request = channel.calls[0]
    assert request.level == Level.DANGER and request.full is not None
    assert "attacker@example.net" in request.full and "attacker" not in request.summary


def test_level3_with_a_truncated_key_shows_complete_input(tmp_path):
    channel = FakeChannel()
    broker = make_broker(tmp_path, channel)
    key = "to" + " " * 50 + "bcc_attacker@example.net"
    ask(broker, "mcp__mail__frob", {key: "x"})
    request = channel.calls[0]
    assert request.level == Level.DANGER and "…(+" in request.summary
    assert request.full is not None and "bcc_attacker@example.net" in request.full


def test_no_channel_or_raising_channel_denies(tmp_path):
    broker = make_broker(tmp_path, None)
    assert not ask(broker, "Write", {"file_path": "a", "content": "x"}).allow

    def boom(_request):
        raise RuntimeError("broken")

    broker.channel = FakeChannel(answer=boom)
    assert not ask(broker, "Write", {"file_path": "a", "content": "x"}).allow


def test_late_yes_does_not_count(tmp_path):
    now = [1000.0]
    channel = FakeChannel(answer=lambda _r: now.__setitem__(0, now[0] + 301) or True)
    broker = make_broker(tmp_path, channel, clock=lambda: now[0], timeout_s=300)
    verdict = ask(broker, "Write", {"file_path": "a", "content": "x"})
    assert not verdict.allow and "timed out" in verdict.message


def test_real_timeout_denies(tmp_path):
    broker = make_broker(tmp_path, FakeChannel(answer=True, delay=1.3), timeout_s=1)
    assert not ask(broker, "Write", {"file_path": "a", "content": "x"}).allow


def test_prompts_are_serialized(tmp_path):
    channel = FakeChannel(answer=True, delay=0.2)
    broker = make_broker(tmp_path, channel)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(ask(broker, "Write", {"file_path": "a", "content": "x"})))
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert channel.max_active == 1 and len(channel.calls) == 2 and all(v.allow for v in results)


def test_permission_tool_itself_is_denied(tmp_path):
    channel = FakeChannel()
    broker = make_broker(tmp_path, channel)
    verdict = ask(broker, PERMISSION_TOOL, {"tool_name": "Write", "input": {}})
    assert not verdict.allow and channel.calls == []


def test_request_outside_a_turn_is_denied(tmp_path):
    channel = FakeChannel()
    broker = make_broker(tmp_path, channel)
    tool_use_id = announce(broker, "Write", {"file_path": "a", "content": "x"})
    broker.end_turn()
    assert not broker.decide("Write", {"file_path": "a", "content": "x"}, tool_use_id).allow
    assert channel.calls == []


def test_forged_or_mismatched_tool_calls_are_denied(tmp_path):
    channel = FakeChannel()
    broker = make_broker(tmp_path, channel)
    data = {"file_path": "a", "content": "x"}
    assert broker.decide("Write", data, "toolu_forged").message == "no matching tool call"
    assert not broker.decide("Write", data, None).allow
    read_id = announce(broker, "Read", {"file_path": "a"})
    assert not broker.decide("Write", data, read_id).allow  # name differs
    tid = announce(broker, "Write", data)
    assert not broker.decide("Write", {"file_path": "a", "content": "y"}, tid).allow  # input differs
    tid = announce(broker, "Write", data)
    other_path = {"file_path": "b", "content": "x"}
    assert not broker.decide("Write", other_path, tid, input_digest(data)).allow  # path differs
    tid = announce(broker, "Write", data)
    assert not broker.decide("Write", data, tid, "0" * 64).allow  # digest differs
    assert channel.calls == []
    tid = announce(broker, "Write", data)
    assert broker.decide("Write", data, tid).allow
    assert not broker.decide("Write", data, tid).allow  # a tool call is reviewed once
    assert len(channel.calls) == 1


def test_announcement_may_arrive_shortly_after_the_request(tmp_path):
    channel = FakeChannel()
    broker = make_broker(tmp_path, channel, bind_wait_s=2)
    data = {"file_path": "a", "content": "x"}
    timer = threading.Timer(0.2, announce, args=(broker, "Write", data, "toolu_late"))
    timer.start()
    assert broker.decide("Write", data, "toolu_late").allow
    timer.join()


def test_flood_is_limited_and_breaker_trips(tmp_path):
    entered = threading.Event()
    gate = threading.Event()

    def answer(_request):
        entered.set()
        gate.wait(5)
        return False

    channel = FakeChannel(answer=answer)
    broker = make_broker(tmp_path, channel)
    results = []

    def worker():
        results.append(ask(broker, "Write", {"file_path": "a", "content": "x"}))

    first = threading.Thread(target=worker)
    first.start()
    assert entered.wait(5)
    waiting = [threading.Thread(target=worker) for _ in range(3)]
    for t in waiting:
        t.start()
    for _ in range(100):
        if broker._waiting == 3:
            break
        time.sleep(0.02)
    assert broker._waiting == 3
    overflow = ask(broker, "Write", {"file_path": "a", "content": "x"})
    assert not overflow.allow and overflow.message == "too many pending approvals"
    gate.set()
    for t in [first, *waiting]:
        t.join(5)
    assert len(channel.calls) == 2  # two refusals, then the rest of the turn is denied
    assert sum(v.message == "too many refused approvals in this turn" for v in results) == 2
    broker.begin_turn()  # a new turn starts fresh
    channel.answer = True
    assert ask(broker, "Write", {"file_path": "a", "content": "x"}).allow


def test_log_has_no_input_or_token(tmp_path, caplog):
    broker = make_broker(tmp_path, FakeChannel())
    broker.token = "tok-" + "z" * 30
    with caplog.at_level("INFO", logger="personal_agent.approval"):
        ask(broker, "Write", {"file_path": "a", "content": "very-secret-content"})
        ask(broker, "mcp__x__evil\nINJECTED", {})
    assert "approval" in caplog.text and "-> allow" in caplog.text
    assert "very-secret-content" not in caplog.text and "zzzz" not in caplog.text
    assert "\nINJECTED" not in caplog.text


# --- console channel -----------------------------------------------------------


def request(level=Level.WRITE, summary='file_path="notes/a.md", content=11 chars: "hello world"', full=None):
    return ApprovalRequest(tool_name="Write", level=level, summary=summary, tool_use_id="t1", full=full)


def run_console(answers, req=None, deadline_s=5.0, **kw):
    out = []
    channel = ConsoleChannel(threading.Lock(), read_line=reader(answers), write=out.append, **kw)
    return channel.ask(req or request(), time.monotonic() + deadline_s), out


@pytest.mark.parametrize(
    "answers, level, expected",
    [
        (["y"], Level.WRITE, True),
        (["JA"], Level.WRITE, True),
        (["n"], Level.WRITE, False),
        ([""], Level.WRITE, False),
        ([None], Level.WRITE, False),
        (["y", "confirm"], Level.DANGER, True),
        (["y", " Confirm "], Level.DANGER, True),
        (["y", "yes"], Level.DANGER, False),
        (["y"], Level.DANGER, False),
    ],
)
def test_console_answers(tty, answers, level, expected):
    assert run_console(answers, request(level))[0] is expected


def test_console_output_is_clean(tty):
    summary = render_input("Write", {"file_path": "a.md", "content": "\x1b[31mhi"})
    ok, out = run_console(["y"], request(summary=summary))
    text = "\n".join(out)
    assert ok and "Write" in text and "level 2 (write)" in text and "\x1b" not in text


def test_console_level3_prints_complete_input(tty):
    ok, out = run_console(["y", "confirm"], request(Level.DANGER, full='{"to": "x"}'))
    assert ok and '{"to": "x"}' in "\n".join(out) and any("confirm" in line for line in out)


def test_console_timeout_denies(tty):
    ok, out = run_console(iter(lambda: None, 1), deadline_s=0.3)
    assert not ok and "[approval timed out: denied]" in out


def test_console_stop_and_busy_lock(tty):
    assert run_console(["y"], stop=lambda: True)[0] is False
    lock = threading.Lock()
    lock.acquire()
    channel = ConsoleChannel(lock, read_line=reader(["y"]), write=lambda _s: None)
    assert channel.ask(request(), time.monotonic() + 0.3) is False


def test_console_without_tty_denies(monkeypatch):
    monkeypatch.setattr(approval, "stdin_is_tty", lambda: False)
    calls = []
    out = []
    channel = ConsoleChannel(threading.Lock(), read_line=lambda t: calls.append(t) or "y", write=out.append)
    assert channel.ask(request(), time.monotonic() + 2) is False
    assert calls == [] and "interactive terminal" in out[0]


def test_typeahead_is_flushed(monkeypatch):
    flushed = []
    monkeypatch.setattr(approval, "termios", SimpleNamespace(TCIFLUSH=0, tcflush=lambda fd, how: flushed.append((fd, how))))
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(fileno=lambda: 7, isatty=lambda: True))
    monkeypatch.setattr(approval, "read_stdin_line", reader(["y"]))
    channel = ConsoleChannel(threading.Lock(), write=lambda _s: None)
    assert channel.ask(request(), time.monotonic() + 2) is True
    assert flushed == [(7, 0)]


def test_failed_flush_denies(monkeypatch):
    def broken(_fd, _how):
        raise OSError("no tty")

    monkeypatch.setattr(approval, "termios", SimpleNamespace(TCIFLUSH=0, tcflush=broken))
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(fileno=lambda: 7, isatty=lambda: True))
    channel = ConsoleChannel(threading.Lock(), read_line=reader(["y"]), write=lambda _s: None)
    assert channel.ask(request(), time.monotonic() + 2) is False


def test_read_stdin_line(monkeypatch):
    r, w = os.pipe()
    with os.fdopen(r, "r") as rf, os.fdopen(w, "w") as wf:
        monkeypatch.setattr(sys, "stdin", rf)
        assert approval.read_stdin_line(0.05) is None
        wf.write("y\n")
        wf.flush()
        assert approval.read_stdin_line(1) == "y"
        wf.close()
        with pytest.raises(EOFError):
            approval.read_stdin_line(1)


# --- socket, files and the MCP child ------------------------------------------


@pytest.fixture
def running(tmp_path):
    channel = FakeChannel()
    broker = make_broker(tmp_path, channel)
    mcp_path = broker.start(tmp_path / "run")
    yield broker, channel, mcp_path
    broker.close()


def test_runtime_files_are_private(running):
    broker, _channel, mcp_path = running
    assert stat.S_IMODE(broker.runtime_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(broker.socket_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(mcp_path.stat().st_mode) == 0o600
    server = json.loads(mcp_path.read_text())["mcpServers"]["pa"]
    assert server["command"] == sys.executable
    assert server["args"][:2] == ["-I", "-S"] and server["args"][2].endswith("approval_server.py")
    assert os.path.isabs(server["args"][2])
    assert set(server["env"]) == {"PA_APPROVAL_SOCKET", "PA_APPROVAL_TOKEN", "PA_APPROVAL_TIMEOUT_S"}
    assert server["env"]["PA_APPROVAL_TOKEN"] == broker.token


def test_ask_broker_over_the_socket(running):
    broker, channel, _ = running
    data = {"file_path": "a.md", "content": "x"}
    tid = announce(broker, "Write", data)
    verdict = ask_broker(str(broker.socket_path), broker.token, "Write", data, tid, 5)
    assert verdict == {"behavior": "allow", "updatedInput": data}
    assert verdict["updatedInput"] is data
    channel.answer = False
    tid = announce(broker, "Write", data)
    denied = ask_broker(str(broker.socket_path), broker.token, "Write", data, tid, 5)
    assert denied["behavior"] == "deny" and denied["message"]


def test_wrong_token_and_missing_socket_deny(running, tmp_path):
    broker, channel, _ = running
    data = {"file_path": "a.md", "content": "x"}
    tid = announce(broker, "Write", data)
    verdict = ask_broker(str(broker.socket_path), "wrong-token", "Write", data, tid, 5)
    assert verdict == {"behavior": "deny", "message": "unauthorized"}
    assert channel.calls == []
    gone = ask_broker(str(tmp_path / "nope"), broker.token, "Write", data, tid, 5)
    assert gone["behavior"] == "deny"
    assert ask_broker("", "", "Write", data, tid, 5)["behavior"] == "deny"


def test_silent_broker_denies(tmp_path):
    path = str(tmp_path / "silent")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)

    def accept_and_close():
        conn, _ = srv.accept()
        conn.recv(65536)
        conn.close()

    t = threading.Thread(target=accept_and_close)
    t.start()
    verdict = ask_broker(path, "tok", "Write", {"file_path": "a"}, "t1", 5)
    t.join()
    srv.close()
    assert verdict == {"behavior": "deny", "message": "approval service unreachable"}


def test_large_inputs(running):
    broker, channel, _ = running
    big = {"file_path": "notes/a.md", "content": "x" * 300_000}
    tid = announce(broker, "Write", big)
    verdict = ask_broker(str(broker.socket_path), broker.token, "Write", big, tid, 5)
    assert verdict["behavior"] == "allow" and verdict["updatedInput"] is big
    assert len(channel.calls) == 1
    long_path = {"file_path": "a" * 5000, "content": "x"}
    assert ask_broker(str(broker.socket_path), broker.token, "Write", long_path, "t", 5)["behavior"] == "deny"
    many = {f"k{i}": i for i in range(51)}
    assert ask_broker(str(broker.socket_path), broker.token, "Write", many, "t", 5)["message"] == "too many fields to review"
    assert len(channel.calls) == 1


def test_shrink_keeps_every_top_level_key_and_whole_paths():
    data = {f"k{i}": "y" * 3000 for i in range(49)}
    data["file_path"] = "p" * 3000
    shrunk = shrink_input(data)
    assert list(shrunk) == list(data)
    assert len(shrunk["file_path"]) == 3000 and len(shrunk["k0"]) == 2000
    nested = shrink_input({"a": [[[[["deep"]]]]], "b": list(range(80))})
    assert nested["a"] == [[["…"]]] and len(nested["b"]) == 50


def test_deeply_nested_frame_is_denied_without_traceback(running, capsys):
    broker, channel, _ = running
    frame = b"[" * 100_000 + b"]" * 100_000 + b"\n"  # valid JSON, under the frame limit
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.connect(str(broker.socket_path))
        s.sendall(frame)
        reply = s.makefile("rb").readline()
    assert json.loads(reply) == {"v": 1, "allow": False, "message": "invalid request"}
    assert "Traceback" not in capsys.readouterr().err
    assert channel.calls == []


def test_input_mismatch_logs_key_names_only(tmp_path, caplog):
    channel = FakeChannel()
    broker = make_broker(tmp_path, channel)
    tid = announce(broker, "Write", {"file_path": "a.md", "content": "announced-secret"})
    with caplog.at_level("WARNING", logger="personal_agent.approval"):
        verdict = broker.decide("Write", {"file_path": "a.md", "content": "forwarded-secret"}, tid)
    assert not verdict.allow and verdict.message == "input differs from the announced tool call"
    assert "keys: content" in caplog.text
    assert "secret" not in caplog.text and channel.calls == []


def test_close_lets_a_running_decision_clean_up(tmp_path):
    cleaned = threading.Event()
    broker = make_broker(tmp_path)

    def answer(_request):
        while not broker.closing.is_set():
            time.sleep(0.01)
        time.sleep(0.3)  # e.g. a remote channel cancelling its pending approval
        cleaned.set()
        return False

    broker.channel = FakeChannel(answer)
    data = {"file_path": "a.md", "content": "x"}
    tid = announce(broker, "Write", data)
    worker = threading.Thread(target=broker.decide, args=("Write", data, tid))
    worker.start()
    while not broker.channel.calls:
        time.sleep(0.01)
    broker.close()
    assert cleaned.is_set()
    worker.join(5)


def test_peer_check_requires_so_peercred(monkeypatch):
    a, b = socket.socketpair(socket.AF_UNIX)
    try:
        assert approval._peer_is_us(a)
        monkeypatch.delattr(socket, "SO_PEERCRED")
        assert not approval._peer_is_us(a)
    finally:
        a.close()
        b.close()


def test_close_removes_everything(tmp_path):
    broker = make_broker(tmp_path, FakeChannel())
    broker.start(tmp_path / "run")
    directory = broker.runtime_dir
    assert directory.exists()
    broker.close()
    broker.close()  # idempotent
    assert not directory.exists()
    assert list((tmp_path / "run").iterdir()) == []


def test_closing_broker_denies(running):
    broker, channel, _ = running
    data = {"file_path": "a.md", "content": "x"}
    tid = announce(broker, "Write", data)
    broker.closing.set()
    assert ask_broker(str(broker.socket_path), broker.token, "Write", data, tid, 5)["behavior"] == "deny"
    assert channel.calls == []


def test_handle_is_pure():
    env = {}
    init = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, env)
    assert init["result"]["protocolVersion"] == approval_server.DEFAULT_PROTOCOL_VERSION
    assert handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, env) is None
    assert handle({"jsonrpc": "2.0", "id": 2, "method": "ping"}, env)["result"] == {}
    call = handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                   "params": {"name": "approve", "arguments": {"tool_name": "Write", "input": {}}}}, env)
    assert json.loads(call["result"]["content"][0]["text"])["behavior"] == "deny"
    bad = handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "other"}}, env)
    assert bad["error"]["code"] == -32602


def test_scrub_environ():
    env = {"PA_APPROVAL_TOKEN": "t", "CLAUDE_CODE_OAUTH_TOKEN": "secret", "PATH": "/bin"}
    approval_server.scrub_environ(env)
    assert env == {"PA_APPROVAL_TOKEN": "t"}


class McpClient:
    def __init__(self, proc):
        self.proc = proc

    def send(self, obj):
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def call(self, msg_id, method, params=None):
        self.send({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})
        reply = json.loads(self.proc.stdout.readline())
        assert reply["id"] == msg_id  # the very next line: notifications got no reply
        return reply

    def approve(self, msg_id, tool_name, tool_input, tool_use_id):
        reply = self.call(msg_id, "tools/call", {"name": "approve", "arguments": {
            "tool_name": tool_name, "input": tool_input, "tool_use_id": tool_use_id}})
        content = reply["result"]["content"]
        assert len(content) == 1 and content[0]["type"] == "text"
        return json.loads(content[0]["text"])


def spawn_child(server, cwd, extra_env=None):
    env = dict(server["env"], **(extra_env or {}))
    return subprocess.Popen(
        [server["command"], *server["args"]], cwd=cwd, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )


def test_mcp_stdio_end_to_end(running, tmp_path):
    broker, channel, mcp_path = running
    server = json.loads(mcp_path.read_text())["mcpServers"]["pa"]
    planted = tmp_path / "planted"
    planted.mkdir()
    marker = tmp_path / "marker"
    (planted / "json.py").write_text(f"open({str(marker)!r}, 'w').close()\n")
    # -I must ignore both the cwd and PYTHONPATH, so the planted json.py never runs.
    proc = spawn_child(server, planted, {"PYTHONPATH": str(planted)})
    client = McpClient(proc)
    try:
        init = client.call(1, "initialize", {"protocolVersion": "2099-01-01"})
        assert init["result"]["protocolVersion"] == "2099-01-01"
        assert "tools" in init["result"]["capabilities"]
        client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools = client.call(2, "tools/list")["result"]["tools"]
        assert [t["name"] for t in tools] == ["approve"]

        big = {"file_path": "notes/a.md", "content": "ä‮" + "x" * 300_000}
        verdict = client.approve(3, "Write", big, announce(broker, "Write", big))
        assert verdict["behavior"] == "allow"
        assert json.dumps(verdict["updatedInput"], sort_keys=True) == json.dumps(big, sort_keys=True)

        channel.answer = False
        small = {"file_path": "b.md", "content": "y"}
        verdict = client.approve(4, "Write", small, announce(broker, "Write", small))
        assert verdict["behavior"] == "deny" and verdict["message"]

        assert client.call(5, "nope/unknown")["error"]["code"] == -32601
        proc.stdin.close()
        assert proc.wait(10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    assert not marker.exists()


def test_mcp_child_with_wrong_socket_denies(running, tmp_path):
    broker, channel, mcp_path = running
    server = json.loads(mcp_path.read_text())["mcpServers"]["pa"]
    server["env"]["PA_APPROVAL_SOCKET"] = str(tmp_path / "missing")
    proc = spawn_child(server, tmp_path)
    try:
        data = {"file_path": "a.md", "content": "x"}
        verdict = McpClient(proc).approve(1, "Write", data, announce(broker, "Write", data))
        assert verdict == {"behavior": "deny", "message": "approval service unreachable"}
        proc.stdin.close()
        proc.wait(10)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert channel.calls == []


# --- chat end to end with the fake CLI ------------------------------------------


@pytest.fixture
def chat(tmp_path, monkeypatch, tty):
    log = tmp_path / "fake.jsonl"
    wrapper = tmp_path / "claude"
    wrapper.write_text(
        f"#!/bin/sh\nFAKE_CLAUDE_LOG={shlex.quote(str(log))} exec "
        f"{shlex.quote(sys.executable)} {shlex.quote(str(FAKE))} \"$@\"\n"
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PA_CLAUDE_BIN", str(wrapper))
    monkeypatch.delenv("PA_WORKDIR", raising=False)
    monkeypatch.delenv("PA_CLAUDE_CONFIG_DIR", raising=False)

    def run(lines, answers):
        from personal_agent.cli import main

        inputs = iter(lines)
        monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
        monkeypatch.setattr(approval, "read_stdin_line", reader(answers))
        return main(["chat"])

    return SimpleNamespace(run=run, log=log, state=tmp_path / "state")


def test_chat_write_is_approved(chat, capsys):
    assert chat.run(["WRITE a.txt", "/exit"], ["y"]) == 0
    out = capsys.readouterr().out
    assert (chat.state / "vault" / "a.txt").read_text() == "x"
    assert "[approval] level 2 (write): Write" in out and "agent[3]> wrote a.txt" in out
    entry = json.loads(chat.log.read_text().splitlines()[0])
    args = entry["args"]
    assert args[args.index("--tools") + 1] == "Read,Glob,Grep,Write,Edit,Skill"
    assert args[args.index("--permission-prompt-tool") + 1] == "mcp__pa__approve"
    assert "--restricted" in args and "--mcp-config" in args
    assert "MCP_TOOL_TIMEOUT" in entry["env"]
    assert list((chat.state / "run").iterdir()) == []  # runtime dir removed on exit


def test_chat_write_is_denied(chat, capsys):
    assert chat.run(["WRITE a.txt", "/exit"], ["n"]) == 0
    out = capsys.readouterr().out
    assert not (chat.state / "vault" / "a.txt").exists()
    assert "[not approved: Write]" in out and "[denied]" in out


def test_chat_plain_turn_still_works(chat, capsys):
    assert chat.run(["hello", "/exit"], []) == 0
    assert "agent[3]> echo: hello" in capsys.readouterr().out


@pytest.mark.parametrize("which", ["package", "home", "state", "root"])
def test_chat_refuses_unsafe_workdir(chat, monkeypatch, tmp_path, capsys, which):
    workdir = {
        "package": Path(approval.__file__).resolve().parents[1],
        "home": Path.home(),
        "state": tmp_path,
        "root": Path("/"),
    }[which]
    monkeypatch.setenv("PA_WORKDIR", str(workdir))
    assert chat.run(["hello", "/exit"], []) == 2
    assert "workdir must not" in capsys.readouterr().err
    assert not chat.log.exists()


@pytest.mark.parametrize("which", ["status_inside", "inside_status", "venv", "site", "claude_bin"])
def test_chat_refuses_workdir_with_trusted_or_executable_content(chat, monkeypatch, tmp_path, capsys, which):
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.setenv("PA_WORKDIR", str(workdir))
    if which == "status_inside":
        monkeypatch.setenv("PA_STATUS_DIR", str(workdir / ".status"))
    elif which == "inside_status":
        monkeypatch.setenv("PA_STATUS_DIR", str(tmp_path))
    elif which == "venv":
        monkeypatch.setattr(sys, "prefix", str(workdir / ".venv"))
    elif which == "site":
        import site

        monkeypatch.setattr(site, "getsitepackages", lambda: [str(workdir / "lib" / "site-packages")])
    else:
        binary = workdir / "claude"
        binary.write_text((tmp_path / "claude").read_text())
        binary.chmod(0o755)
        monkeypatch.setenv("PA_CLAUDE_BIN", str(binary))
    assert chat.run(["hello", "/exit"], []) == 2
    assert "workdir must not" in capsys.readouterr().err
    assert not chat.log.exists()


def test_chat_error_while_a_prompt_is_open_ends_the_prompt(chat, monkeypatch, capsys):
    monkeypatch.setenv("PA_APPROVAL_TIMEOUT_S", "30")

    def never_answers(timeout):
        time.sleep(timeout)
        return None

    from personal_agent.cli import main

    inputs = iter(["WRITE_DIE a.txt", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    monkeypatch.setattr(approval, "read_stdin_line", never_answers)
    started = time.monotonic()
    assert main(["chat"]) == 0
    elapsed = time.monotonic() - started
    out = capsys.readouterr().out
    assert "[approval] level 2 (write): Write" in out  # the prompt really was open
    assert "[error]" in out and "[approved]" not in out
    assert elapsed < 10, elapsed  # not the 30 s approval timeout
    assert not (chat.state / "vault" / "a.txt").exists()


# --- configuration -------------------------------------------------------------


def test_approval_timeout_config(monkeypatch, tmp_path):
    monkeypatch.setenv("PA_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("PA_APPROVAL_TIMEOUT_S", raising=False)
    assert Config.from_env().approval_timeout_s == 300
    monkeypatch.setenv("PA_APPROVAL_TIMEOUT_S", "60")
    assert Config.from_env().approval_timeout_s == 60
    for bad in ("5", "4000", "abc"):
        monkeypatch.setenv("PA_APPROVAL_TIMEOUT_S", bad)
        with pytest.raises(ConfigError):
            Config.from_env()
