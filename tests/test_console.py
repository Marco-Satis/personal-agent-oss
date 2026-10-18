import threading
import time

from personal_agent.console import ChatConsole


def test_non_interactive_uses_input(monkeypatch):
    lines = []
    console = ChatConsole(threading.Lock(), interactive=False, write=lines.append)
    inputs = iter(["hello"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    console.notice("[task abc123 done: outputs/tasks/abc123.md]")
    assert console.read_message("you> ") == "hello"
    assert lines == ["[task abc123 done: outputs/tasks/abc123.md]"]  # notices flushed before reading

    def eof(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert console.read_message("you> ") is None


def test_interactive_reader_yields_to_a_waiting_channel():
    lock = threading.Lock()
    out = []
    answer = threading.Event()

    def read_line(timeout):
        time.sleep(timeout)
        return "typed" if answer.is_set() else None

    console = ChatConsole(lock, interactive=True, read_line=read_line, write=out.append, poll_s=0.05)
    result = {}
    reader = threading.Thread(target=lambda: result.update(line=console.read_message("you> ")))
    reader.start()
    time.sleep(0.15)
    assert console.reading
    with console.wanted():
        started = time.monotonic()
        assert lock.acquire(timeout=1)
        waited = time.monotonic() - started
        out.append("[approval · task abc123] level 2 (write): Edit")
        lock.release()
    assert waited < 2 * 0.05 + 0.05
    answer.set()
    reader.join(2)
    assert result["line"] == "typed"
    assert out.count("you> ") >= 2  # the prompt is printed again after the approval prompt


def test_say_and_notices_mark_the_prompt_for_reprint():
    lock = threading.Lock()
    out = []
    calls = iter([None, None, "x"])
    console = ChatConsole(lock, interactive=True, read_line=lambda t: next(calls), write=out.append, poll_s=0.01)
    console.notice("[task abc123 cancelled]")
    assert console.read_message("you> ") == "x"
    assert out[0] == "[task abc123 cancelled]" and out[1] == "you> "
    console.say("agent[1]> ok")
    assert out[-1] == "agent[1]> ok"


def test_interactive_eof_returns_none():
    def read_line(timeout):
        raise EOFError

    console = ChatConsole(threading.Lock(), interactive=True, read_line=read_line, write=lambda s: None, poll_s=0.01)
    assert console.read_message("you> ") is None
    assert not console.reading


def test_notice_never_blocks_while_the_lock_is_held():
    lock = threading.Lock()
    out = []
    console = ChatConsole(lock, interactive=False, write=out.append)
    with lock:  # e.g. an approval prompt is on screen
        started = time.monotonic()
        console.notice("[task abc123 done: outputs/tasks/abc123.md]")
        assert time.monotonic() - started < 0.1
    console.flush()
    assert out == ["[task abc123 done: outputs/tasks/abc123.md]"]
