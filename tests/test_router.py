import json
import shlex
import stat
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from personal_agent.bridge import TurnResult
from personal_agent.config import Config, ConfigError
from personal_agent.quota import PROBE_AFTER_S, QuotaGuard, parse_reset, summarize
from personal_agent.router import Router, RouterConfig, choose_tier
from personal_agent.status import SessionStatus

FAKE = Path(__file__).resolve().parent / "fake_claude.py"
NOW = 1_800_000_000.0


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


def info(status="allowed", five=None, seven=None, five_reset=None, seven_reset=None, **extra):
    windows = {}
    if five is not None:
        windows["five_hour"] = {"utilization": five, "resetsAt": five_reset}
    if seven is not None:
        windows["seven_day"] = {"utilization": seven, "resetsAt": seven_reset}
    return {"status": status, "unifiedWindows": windows, **extra}


def guard_with(data, stop=0.9, clock=None, state_file=None):
    guard = QuotaGuard(stop, state_file, clock=clock or Clock())
    if data is not None:
        guard.observe(data)
    return guard


# --- quota guard -----------------------------------------------------------------


def test_no_data_is_ok():
    check = guard_with(None).check()
    assert check.ok and "no quota data" in check.reason


@pytest.mark.parametrize(
    "data, ok",
    [
        (info(five=0.5, seven=0.3), True),
        (info(five=0.9, five_reset=NOW + 3600), False),  # >= stop refuses
        (info(five=0.89, seven=0.95, seven_reset=NOW + 86400), False),
        (info(status="rejected", resetsAt=NOW + 600), False),
        (info(status="rejected", resetsAt=NOW - 1), True),  # reset in the past: one probe turn
        (info(five=0.95, five_reset=NOW - 1), True),
        (info(five=0.2, isUsingOverage=True, resetsAt=NOW + 600), False),
        (info(five=0.2, overageInUse=True), False),
        (info(status="allowed_warning", five=0.5, resetsAt=NOW + 600), True),  # approaching only
        (info(status="allowed_warning", five=0.93, five_reset=NOW + 600), False),  # threshold decides
        (info(status="overage_unknown", resetsAt=NOW + 600), False),  # unknown status blocks
        ({"unifiedWindows": {"five_hour": {"utilization": 0.1}}}, False),  # missing status
    ],
)
def test_guard_thresholds_and_status(data, ok):
    check = guard_with(data).check()
    assert check.ok is ok, check.reason


def test_refusal_reason_names_window_and_threshold():
    reason = guard_with(info(five=0.92, five_reset=NOW + 3600)).check().reason
    assert "five-hour window at 92 %" in reason and "stop at 90 %" in reason and "resets" in reason


def test_unknown_reset_blocks_until_probe_time():
    clock = Clock()
    guard = guard_with(info(status="rejected"), clock=clock)
    clock.now = NOW + 10 * 60
    assert not guard.check().ok
    clock.now = NOW + 20 * 60
    assert guard.check().ok
    assert PROBE_AFTER_S == 15 * 60


def test_overage_without_reset_has_no_probe_turn_until_quota_reset(tmp_path):
    clock = Clock()
    state = tmp_path / "quota.json"
    guard = guard_with(info(five=0.2, isUsingOverage=True), clock=clock, state_file=state)
    clock.now = NOW + 3 * 3600  # far beyond the 15-minute probe
    check = guard.check()
    assert not check.ok and "quota reset" in check.reason
    guard.reset()
    assert guard.check().ok and not state.exists()
    assert QuotaGuard(0.9, state, clock=clock).check().reason == "no quota data yet"


def test_overage_blocks_until_the_latest_known_window_reset():
    clock = Clock()
    guard = guard_with(
        info(five=0.2, five_reset=NOW + 3600, seven=0.5, seven_reset=NOW + 7200, isUsingOverage=True), clock=clock
    )
    clock.now = NOW + 5400  # past the five-hour reset and the probe time, before the seven-day reset
    assert not guard.check().ok
    clock.now = NOW + 7201
    assert guard.check().ok


def test_quota_reset_keeps_a_paid_usage_block():
    guard = guard_with(info(five=0.1))
    guard.block_paid("api key in use")
    guard.reset()
    assert guard.check().reason == "api key in use"


def test_custom_stop_threshold():
    assert not guard_with(info(five=0.5), stop=0.5).check().ok
    assert guard_with(info(five=0.49), stop=0.5).check().ok


@pytest.mark.parametrize(
    "data",
    [
        info(five="0.99"),
        {"status": "allowed", "unifiedWindows": [1, 2]},
        {"status": 5, "unifiedWindows": {"five_hour": "x", "seven_day": None}},
        {"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": True, "resetsAt": "soon"}}},
        "garbage",
        None,
        [],
    ],
)
def test_malformed_info_never_raises(data):
    guard = guard_with(data)
    guard.check()
    guard.describe()


def test_parse_reset():
    assert parse_reset(1_800_000_000) == 1_800_000_000
    assert parse_reset(1_800_000_000_000) == 1_800_000_000
    assert parse_reset("1800000000") == 1_800_000_000
    assert parse_reset("2027-01-15T10:00:00Z") == parse_reset("2027-01-15T10:00:00+00:00")
    assert parse_reset("2027-01-15T10:00:00Z") > 1_700_000_000
    for garbage in ("soon", None, [], {}, True, -5, float("nan")):
        assert parse_reset(garbage) is None


def test_summarize_keeps_only_whitelisted_fields():
    summary = summarize(info(five=0.3, five_reset=NOW, seven=0.1, extra_field="secret", rateLimitType="five_hour"))
    assert set(summary) == {"status", "resetsAt", "isUsingOverage", "windows"}
    assert set(summary["windows"]["five_hour"]) == {"utilization", "resetsAt"}
    assert "secret" not in json.dumps(summary)


def test_describe():
    text = guard_with(info(five=0.29, five_reset=NOW + 3600, seven=0.41)).describe()
    assert "five-hour window 29 % (resets" in text and "seven-day window 41 %" in text
    assert "status allowed" in text and "stop at 90 %" in text
    assert "no quota data yet" in guard_with(None).describe()
    assert "blocked:" in guard_with(info(five=0.95)).describe()


def test_paid_block_is_sticky():
    guard = guard_with(info(five=0.1))
    guard.block_paid("api key in use")
    guard.observe(info(five=0.1))
    check = guard.check()
    assert not check.ok and check.reason == "api key in use"


# --- persistence -------------------------------------------------------------------


def test_state_file_roundtrip(tmp_path):
    state = tmp_path / "quota.json"
    guard_with(info(five=0.95, five_reset=NOW + 3600, extra="drop me"), state_file=state)
    saved = json.loads(state.read_text())
    assert set(saved) == {"observed_at", "status", "resetsAt", "isUsingOverage", "windows"}
    assert "drop me" not in state.read_text()
    assert [p.name for p in tmp_path.iterdir()] == ["quota.json"]  # no .tmp leftovers
    assert not QuotaGuard(0.9, state, clock=Clock()).check().ok


@pytest.mark.parametrize("content", ["{not json", "[]", '{"observed_at": "x"}', ""])
def test_corrupt_state_file_means_no_data(tmp_path, content):
    state = tmp_path / "quota.json"
    state.write_text(content)
    assert QuotaGuard(0.9, state, clock=Clock()).check().reason == "no quota data yet"


def test_hostile_state_values_are_revalidated(tmp_path):
    state = tmp_path / "quota.json"
    state.write_text(json.dumps({"observed_at": NOW, "status": "allowed", "windows": {"five_hour": {"utilization": "1"}}}))
    assert QuotaGuard(0.9, state, clock=Clock()).check().ok


# --- tier selection -----------------------------------------------------------------


CFG = RouterConfig()


@pytest.mark.parametrize(
    "text, expected",
    [
        ("help", (1, "help")),
        ("/help", (1, "help")),
        ("?", (1, "help")),
        ("  Status ", (1, "status")),
        ("quota", (1, "quota")),
        ("kontingent", (1, "quota")),
        ("Quota Reset", (1, "quota_reset")),
        ("!status", (3, "status")),
        ("! What is the capital of France?", (3, "What is the capital of France?")),
        ("What is the capital of France?", (2, "What is the capital of France?")),
        ("Wie spät ist es in Tokio?", (2, "Wie spät ist es in Tokio?")),
        ("What is 2+2?", (2, "What is 2+2?")),
        ("translate good morning to French", (2, "translate good morning to French")),
        ("Write a note to notes.md", (3, "Write a note to notes.md")),
        ("what's in notes.md?", (3, "what's in notes.md?")),
        ("can you read it again?", (3, "can you read it again?")),
        ("what about that?", (3, "what about that?")),
        ("what is at https://example.org?", (3, "what is at https://example.org?")),
        ("Why? Explain it.", (3, "Why? Explain it.")),
        ("hello", (3, "hello")),
        ("status of the session?", (3, "status of the session?")),
    ],
)
def test_choose_tier(text, expected):
    assert choose_tier(text, CFG) == expected


def test_choose_tier_length_lines_and_disabled_small_model():
    long_question = "what " + "x" * 194 + "?"
    assert len(long_question) == 200
    assert choose_tier(long_question, CFG)[0] == 3
    assert choose_tier("what is\nthis?", CFG)[0] == 3
    assert choose_tier("What is the capital of France?", RouterConfig(small_model=None))[0] == 3
    assert choose_tier("What is the capital of France?", RouterConfig(small_max_chars=20))[0] == 3


# --- router with stub sessions ------------------------------------------------------


class StubSession:
    def __init__(self, name, events=()):
        self.name = name
        self.events = list(events)
        self.sent = []
        self.closed = []
        self.last_rate_limit = None

    def send(self, text, on_event=None):
        self.sent.append(text)
        for event in self.events:
            if on_event is not None:
                on_event(event)
        return TurnResult(f"{self.name}: {text}", "sid", False, "success", [], 0.0, None)

    def close(self, timeout_s=10):
        self.closed.append(timeout_s)


def make_router(main=None, small=None, guard=None, config=CFG, statuses=()):
    main = main or StubSession("main")
    return Router(main, small, guard or guard_with(None), config, statuses=lambda: list(statuses))


def test_router_tiers():
    main, small = StubSession("main"), StubSession("small")
    router = make_router(main, small)
    assert router.handle("What is the capital of France?").tier == 2
    assert router.handle("hello").tier == 3
    reply = router.handle("!What is 2+2?")
    assert reply.tier == 3 and reply.text == "main: What is 2+2?" and reply.result is not None
    assert small.sent == ["What is the capital of France?"]
    assert main.sent == ["hello", "What is 2+2?"]
    router.close()
    assert main.closed and small.closed


def test_router_without_small_session_uses_main():
    main = StubSession("main")
    reply = make_router(main, None).handle("What is the capital of France?")
    assert reply.tier == 3 and main.sent == ["What is the capital of France?"]


def test_tier1_answers_without_any_session():
    main, small = StubSession("main"), StubSession("small")
    status = SessionStatus("s1", "demo-project", "waiting", "needs input", time.time(), time.time() - 120)
    router = make_router(main, small, guard=guard_with(info(five=0.29, seven=0.41)), statuses=[status])
    help_reply = router.handle("help")
    assert help_reply.tier == 1 and '"!"' in help_reply.text
    status_reply = router.handle("status")
    assert "demo-project" in status_reply.text and "waiting" in status_reply.text
    assert "five-hour window 29 %" in router.handle("quota").text
    assert "no session status" in make_router().handle("sessions").text
    assert main.sent == [] and small.sent == []


def test_quota_reset_command_unblocks_overage_without_a_model(tmp_path):
    main = StubSession("main")
    state = tmp_path / "quota.json"
    router = make_router(main, guard=guard_with(info(five=0.2, isUsingOverage=True), state_file=state))
    assert router.handle("hello").refused
    reply = router.handle("quota reset")
    assert reply.tier == 1 and "cleared" in reply.text and not state.exists()
    assert router.handle("hello").tier == 3 and main.sent == ["hello"]


def test_refused_guard_sends_nothing():
    main, small = StubSession("main"), StubSession("small")
    router = make_router(main, small, guard=guard_with(info(five=0.95)))
    for text in ("hello", "What is the capital of France?", "!hello"):
        reply = router.handle(text)
        assert reply.refused and reply.text.startswith("Not sent: five-hour window at 95 %")
    assert main.sent == [] and small.sent == []
    assert router.handle("quota").tier == 1  # fixed answers still work


def test_rate_limit_event_is_observed_and_forwarded():
    event = {"type": "rate_limit_event", "rate_limit_info": info(five=0.95, five_reset=NOW + 3600)}
    guard = guard_with(None)
    seen = []
    router = make_router(StubSession("main", [event]), guard=guard)
    assert not router.handle("hello", on_event=seen.append).refused
    assert seen == [event]
    assert not guard.check().ok
    assert router.handle("hello again").refused


def test_api_key_source_aborts_and_blocks():
    init = {"type": "system", "subtype": "init", "apiKeySource": "/login managed key"}
    main = StubSession("main", [init])
    router = make_router(main)
    reply = router.handle("hello")
    assert reply.refused and "API key source" in reply.text
    assert main.closed == [0]
    assert router.handle("hello again").refused
    assert main.sent == ["hello"]


def test_subscription_key_source_passes():
    init = {"type": "system", "subtype": "init", "apiKeySource": "none"}  # pragma: allowlist secret
    assert not make_router(StubSession("main", [init])).handle("hello").refused


def test_overage_during_turn_aborts():
    event = {"type": "rate_limit_event", "rate_limit_info": info(five=0.5, isUsingOverage=True, resetsAt=NOW + 3600)}
    main = StubSession("main", [event])
    guard = guard_with(None)
    reply = make_router(main, guard=guard).handle("hello")
    assert reply.refused and "overage" in reply.text and main.closed == [0]
    assert not guard.check().ok


def test_force_prefix_alone_sends_nothing():
    main = StubSession("main")
    reply = make_router(main).handle("!")
    assert reply.is_error and main.sent == []


# --- chat end to end with the fake CLI ----------------------------------------------


@pytest.fixture
def chat(tmp_path, monkeypatch):
    log = tmp_path / "fake.jsonl"
    wrapper = tmp_path / "claude"
    state = tmp_path / "state"

    def make_wrapper(extra_env=""):
        wrapper.write_text(
            f"#!/bin/sh\n{extra_env}FAKE_CLAUDE_LOG={shlex.quote(str(log))} exec "
            f"{shlex.quote(sys.executable)} {shlex.quote(str(FAKE))} \"$@\"\n"
        )
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)

    make_wrapper()
    monkeypatch.setenv("PA_STATE_DIR", str(state))
    monkeypatch.setenv("PA_CLAUDE_BIN", str(wrapper))
    for name in ("PA_WORKDIR", "PA_CLAUDE_CONFIG_DIR", "PA_STATUS_DIR", "PA_QUOTA_STOP",
                 "PA_ROUTER_SMALL_MODEL", "PA_ROUTER_SMALL_MAX_CHARS", "PA_CLAUDE_MODEL"):
        monkeypatch.delenv(name, raising=False)

    def run(lines):
        from personal_agent.cli import main

        inputs = iter(lines)
        monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
        return main(["chat"])

    def starts():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return SimpleNamespace(run=run, log=log, state=state, starts=starts, make_wrapper=make_wrapper)


def test_chat_tier3_by_default(chat, capsys):
    assert chat.run(["hello", "/exit"]) == 0
    assert "agent[3]> echo: hello" in capsys.readouterr().out
    assert json.loads((chat.state / "quota.json").read_text())["windows"]["five_hour"]["utilization"] == 0.25


def test_chat_tier1_starts_no_process(chat, capsys):
    sessions = chat.state / "sessions"
    sessions.mkdir(parents=True)
    now = time.time()
    (sessions / "a.json").write_text(json.dumps(
        {"session_id": "a", "project": "demo-project", "state": "waiting", "summary": "needs input",
         "updated_at": now, "since": now - 300}))
    assert chat.run(["status", "help", "quota", "/exit"]) == 0
    out = capsys.readouterr().out
    assert "agent[1]> waiting" in out and "demo-project" in out
    assert "agent[1]> Commands answered without a model" in out
    assert "agent[1]> no quota data yet" in out
    assert not chat.log.exists()


def test_chat_tier2_uses_small_model_without_tools(chat, capsys):
    assert chat.run(["What is 2+2?", "/exit"]) == 0
    assert "agent[2]> echo: What is 2+2?" in capsys.readouterr().out
    [start] = chat.starts()
    args = start["args"]
    assert args[args.index("--model") + 1] == "haiku"
    assert args[args.index("--tools") + 1] == ""
    assert "--restricted" in args and "--strict-mcp-config" in args
    assert "--mcp-config" not in args and "--permission-prompt-tool" not in args


def test_chat_small_model_can_be_disabled(chat, capsys, monkeypatch):
    monkeypatch.setenv("PA_ROUTER_SMALL_MODEL", "off")
    assert chat.run(["What is 2+2?", "/exit"]) == 0
    assert "agent[3]> echo: What is 2+2?" in capsys.readouterr().out
    args = chat.starts()[0]["args"]
    assert args[args.index("--tools") + 1] == "Read,Glob,Grep,Write,Edit"


def test_chat_refuses_over_quota_without_starting(chat, capsys):
    chat.state.mkdir(parents=True)
    (chat.state / "quota.json").write_text(json.dumps({
        "observed_at": time.time(), "status": "allowed", "resetsAt": None, "isUsingOverage": False,
        "windows": {"five_hour": {"utilization": 0.95, "resetsAt": time.time() + 3600}}}))
    assert chat.run(["hello", "What is 2+2?", "quota", "/exit"]) == 0
    out = capsys.readouterr().out
    assert out.count("[quota] Not sent: five-hour window at 95 %") == 2
    assert "agent[1]> five-hour window 95 %" in out
    assert not chat.log.exists()


def test_chat_api_key_source_stops_everything(chat, capsys):
    chat.make_wrapper("FAKE_CLAUDE_API_KEY_SOURCE=ANTHROPIC_API_KEY ")
    assert chat.run(["hello", "hello again", "/exit"]) == 0
    out = capsys.readouterr().out
    assert "[quota] Stopped: Claude Code reports API key source 'ANTHROPIC_API_KEY'" in out
    assert "[quota] Not sent:" in out and "echo: hello" not in out
    assert len(chat.starts()) == 1  # no second process, no second send


def test_chat_overage_aborts_turn_quickly(chat, capsys):
    started = time.monotonic()
    assert chat.run(["OVERAGE", "hello", "/exit"]) == 0
    assert time.monotonic() - started < 4  # the fake would pause 5 s before answering
    out = capsys.readouterr().out
    assert "[quota] Stopped: paid overage is in use" in out
    assert "[quota] Not sent: paid overage is in use" in out
    assert len(chat.starts()) == 1


# --- configuration ------------------------------------------------------------------


def test_router_config(monkeypatch, tmp_path):
    monkeypatch.setenv("PA_STATE_DIR", str(tmp_path))
    for name in ("PA_QUOTA_STOP", "PA_ROUTER_SMALL_MODEL", "PA_ROUTER_SMALL_MAX_CHARS"):
        monkeypatch.delenv(name, raising=False)
    config = Config.from_env()
    assert (config.quota_stop, config.router_small_model, config.router_small_max_chars) == (0.9, "haiku", 160)
    monkeypatch.setenv("PA_QUOTA_STOP", "0.75")
    monkeypatch.setenv("PA_ROUTER_SMALL_MODEL", "OFF")
    monkeypatch.setenv("PA_ROUTER_SMALL_MAX_CHARS", "300")
    config = Config.from_env()
    assert (config.quota_stop, config.router_small_model, config.router_small_max_chars) == (0.75, None, 300)
    for value in ("none", "0"):
        monkeypatch.setenv("PA_ROUTER_SMALL_MODEL", value)
        assert Config.from_env().router_small_model is None


@pytest.mark.parametrize("name, value", [
    ("PA_QUOTA_STOP", "abc"),
    ("PA_QUOTA_STOP", "1.5"),
    ("PA_QUOTA_STOP", "0"),
    ("PA_QUOTA_STOP", "nan"),
    ("PA_ROUTER_SMALL_MAX_CHARS", "5"),
    ("PA_ROUTER_SMALL_MAX_CHARS", "x"),
])
def test_router_config_errors(monkeypatch, tmp_path, name, value):
    monkeypatch.setenv("PA_STATE_DIR", str(tmp_path))
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError):
        Config.from_env()
