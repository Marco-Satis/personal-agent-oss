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
TASK_ENV_NAMES = ("PA_TASKS", "PA_TASK_PARALLEL", "PA_TASK_MODEL", "PA_TASK_TIMEOUT_S", "PA_TASK_QUOTA_STOP",
                  "PA_TASK_DISCORD", "PA_TASK_DELEGATION", "PA_TASK_MAX_AGE_H", "PA_DOMAIN_CONTEXT_CHARS",
                  "PA_SESSIONS_SOURCE")


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
                 "PA_ROUTER_SMALL_MODEL", "PA_ROUTER_SMALL_MAX_CHARS", "PA_CLAUDE_MODEL", *TASK_ENV_NAMES):
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


def test_chat_sessions_reads_only_the_hook_files_by_default(chat, capsys):
    sessions = chat.state / "sessions"
    sessions.mkdir(parents=True)
    now = time.time()
    for sid, state in (("a", "idle"), ("b", "waiting")):
        (sessions / f"{sid}.json").write_text(json.dumps(
            {"session_id": sid, "project": f"proj-{sid}", "state": state, "summary": "",
             "updated_at": now, "since": now - 300}))
    assert chat.run(["sessions", "/exit"]) == 0
    out = capsys.readouterr().out
    assert "agent[1]> 2 sessions (1 waiting)" in out
    assert out.index("proj-b") < out.index("proj-a")  # waiting first
    assert not chat.log.exists()  # neither a model nor `claude agents` was started


def test_chat_sessions_with_the_agents_source(chat, capsys, monkeypatch):
    listing = [{"cwd": "/home/person/secret/proj-z", "kind": "background", "id": "bg-1", "state": "blocked"}]
    chat.make_wrapper(f"FAKE_CLAUDE_AGENTS_JSON={shlex.quote(json.dumps(listing))} ")
    monkeypatch.setenv("PA_SESSIONS_SOURCE", "both")
    assert chat.run(["sessions", "/exit"]) == 0
    out = capsys.readouterr().out
    assert "agent[1]> 1 sessions (1 waiting)" in out and "proj-z" in out and "background" in out
    assert "/home/person" not in out
    [start] = chat.starts()
    assert start["args"] == ["agents", "--json"]
    assert not {"CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "PA_API_TOKEN"} & set(start["env"])


def test_chat_domain_commands_start_no_process(chat, capsys):
    folder = chat.state / "vault" / "wiki" / "domains"
    folder.mkdir(parents=True, mode=0o700)
    chat.state.chmod(0o700)
    (folder / "research.md").write_text("# Domain: research\n\nLinks: finance\n\n## Notes\n- a finding\n")
    assert chat.run(["domains", "domain research", "domain nope", "/exit"]) == 0
    out = capsys.readouterr().out
    assert "agent[1]> research             1 links  1 notes" in out
    assert "agent[1]> domain research · wiki/domains/research.md" in out and "- a finding" in out
    assert "agent[1]> no domain nope" in out
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
    assert args[args.index("--tools") + 1] == "Read,Glob,Grep,Write,Edit,Skill"


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


# --- vault: note capture, slash commands, excerpts ------------------------------------


from datetime import datetime  # noqa: E402

from personal_agent import approval  # noqa: E402
from personal_agent import router as router_mod  # noqa: E402
from personal_agent.router import prefix_command, slash_problem  # noqa: E402
from personal_agent.vault import Vault  # noqa: E402


@pytest.fixture
def vault(tmp_path):
    v = Vault(tmp_path / "vault")
    v.init()
    return v


def vault_router(vault, main=None, small=None, context_chars=3000, slash=(), **kw):
    main = main or StubSession("main")
    return Router(main, small, guard_with(None), CFG, statuses=lambda: [], vault=vault,
                  context_chars=context_chars, slash_commands=frozenset(slash), **kw)


def test_note_is_captured_at_tier1_without_a_model(vault):
    main, small = StubSession("main"), StubSession("small")
    router = vault_router(vault, main, small)
    reply = router.handle("note buy milk")
    today = datetime.now().strftime("%Y-%m-%d")
    assert reply.tier == 1 and not reply.is_error and reply.text == f"noted in raw/inbox-{today}.md"
    assert router.handle("Notiz Brot kaufen").tier == 1
    assert router.handle("/note third").tier == 1
    lines = (vault.root / "raw" / f"inbox-{today}.md").read_text().splitlines()
    assert lines[0] == f"# Inbox {today}" and lines[1] == ""
    assert [line[8:] for line in lines[2:]] == ["buy milk", "Brot kaufen", "third"]
    assert lines[2][:2] == "- " and lines[2][4] == ":"
    assert main.sent == [] and small.sent == []


def test_note_usage_errors_and_force_prefix(vault):
    main = StubSession("main")
    router = vault_router(vault, main, context_chars=0)
    assert router.handle("note").text == "usage: note <text>"
    assert router.handle("NOTE  ").text == "usage: note <text>"
    long = router.handle("note " + "x" * 2001)
    assert long.is_error and "2000" in long.text
    multi = router.handle("note line one\nline\x07 two")
    assert not multi.is_error
    assert router.handle("!note x").tier == 3 and main.sent == ["note x"]
    assert prefix_command("notes are fine") is None and prefix_command("!note x") is None


def test_note_without_vault():
    reply = make_router().handle("note x")
    assert reply.tier == 1 and reply.is_error and reply.text == "no vault configured"


def test_note_needs_no_quota(vault):
    main = StubSession("main")
    router = Router(main, None, guard_with(info(five=0.99)), CFG, statuses=lambda: [], vault=vault)
    assert not router.handle("note offline idea").is_error
    assert router.handle("hello").refused


@pytest.mark.parametrize("text", [
    "/config", "/unknown:x", "!/config", "! /clear", "​/config", "/core", "/core:nonexistent",
    "!⁦ /compact", " \n/clear", "/core:daily-plan-x", "/CORE:daily-plan",
])
def test_slash_commands_are_refused_without_a_session(vault, text):
    main, small = StubSession("main"), StubSession("small")
    reply = vault_router(vault, main, small, slash=["/core:daily-plan"]).handle(text)
    assert reply.tier == 1 and reply.is_error and "unknown command" in reply.text
    assert main.sent == [] and small.sent == []


def test_allowed_plugin_command_goes_verbatim_to_tier3(vault):
    (vault.root / "wiki" / "daily.md").write_text("daily plan notes")
    main, small = StubSession("main"), StubSession("small")
    router = vault_router(vault, main, small, slash=["/core:daily-plan"])
    reply = router.handle("/core:daily-plan today please")
    assert reply.tier == 3 and reply.context_count == 0
    assert main.sent == ["/core:daily-plan today please"] and small.sent == []
    assert router.handle("!​/core:daily-plan").tier == 3
    assert main.sent[-1] == "/core:daily-plan"


def test_plugin_command_marks_the_approval_context(vault):
    main = StubSession("main")
    seen = []
    router = vault_router(vault, main, slash=["/core:organize-inbox"], on_context=seen.append)
    assert router.handle("/core:organize-inbox").tier == 3
    # The CLI expands the command itself (no Skill tool call), so the router marks the turn.
    assert seen == [router_mod.SKILL_MARKER]
    assert router_mod.SKILL_MARKER == approval.SKILL_CONTEXT
    seen.clear()
    router.handle("/config")  # refused: nothing sent, nothing marked
    assert seen == []


def test_plugin_command_marker_reaches_a_write_approval(vault):
    seen = []

    class Channel:
        def ask(self, request, deadline):
            seen.append(request)
            return True

    class SkillSession(StubSession):
        """Like the CLI expanding /core:organize-inbox: the skill asks for one Write."""

        def send(self, text, on_event=None):
            result = super().send(text, on_event)
            tool_input = {"file_path": "wiki/garden.md", "content": "x"}
            broker.observe({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "toolu_1", "name": "Write", "input": tool_input}]}})
            assert broker.decide("Write", tool_input, "toolu_1").allow
            return result

    broker = approval.ApprovalBroker(vault.root, channel=Channel(), bind_wait_s=0.2)
    router = vault_router(vault, SkillSession("main"), slash=["/core:organize-inbox"],
                          on_context=broker.set_turn_context)
    broker.begin_turn()
    assert router.handle("/core:organize-inbox").tier == 3
    assert seen[-1].context == approval.SKILL_CONTEXT


def test_slash_problem_helper():
    allowed = frozenset({"/core:capture-note"})
    assert slash_problem("hello /config", allowed) is None
    assert slash_problem("/core:capture-note buy milk", allowed) is None
    assert slash_problem("﻿/core:capture-note", allowed) is None
    assert slash_problem("/core:capture-note​ x", allowed)  # invisible inside the word
    assert slash_problem("\U000e0020/help", allowed)


def test_tier3_gets_delimited_excerpts_tier2_never(vault):
    (vault.root / "wiki" / "boiler.md").write_text("# Boiler\n\nThe boiler service is due in November.")
    main, small = StubSession("main"), StubSession("small")
    seen = []
    router = vault_router(vault, main, small, on_context=seen.append)
    reply = router.handle("remind me about the boiler service")
    assert reply.tier == 3 and reply.context_count >= 1
    sent = main.sent[-1]
    assert sent.startswith('<vault-excerpts id="') and 'path="wiki/boiler.md"' in sent
    assert sent.endswith("<user-message>\nremind me about the boiler service\n</user-message>")
    assert seen == [router_mod.CONTEXT_MARKER]
    assert router.handle("When is the boiler service due?").tier == 2
    assert small.sent == ["When is the boiler service due?"]
    forced = router.handle("!boiler")
    assert forced.tier == 3 and main.sent[-1].startswith("<vault-excerpts")
    unrelated = router.handle("hello there")
    assert unrelated.context_count == 0 and main.sent[-1] == "hello there"


def test_context_chars_zero_sends_plain_text(vault):
    (vault.root / "wiki" / "boiler.md").write_text("boiler service")
    main = StubSession("main")
    reply = vault_router(vault, main, context_chars=0).handle("boiler service plans")
    assert reply.context_count == 0 and main.sent == ["boiler service plans"]


def test_too_long_prompt_drops_excerpts(vault):
    (vault.root / "wiki" / "boiler.md").write_text("boiler " + "z" * 1000)
    main = StubSession("main")
    message = "boiler " + "w " * 9_900
    reply = vault_router(vault, main, context_chars=12_000).handle(message)
    assert reply.context_count == 0 and main.sent == [message.strip()]


def test_broken_vault_selection_keeps_the_turn(vault, monkeypatch):
    def boom(*args, **kw):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(vault, "select_context", boom)
    main = StubSession("main")
    reply = vault_router(vault, main).handle("boiler service plans")
    assert reply.tier == 3 and main.sent == ["boiler service plans"]


def test_help_mentions_note(vault):
    text = vault_router(vault, slash=["/core:daily-plan"]).handle("help").text
    assert "note <text>" in text and "/core:daily-plan" in text
    assert "note <text>" not in make_router().handle("help").text


# --- safety tripwires (plugins, hooks, MCP servers, skills) ------------------------------

from personal_agent.bridge import BridgeConfig  # noqa: E402
from personal_agent.router import (  # noqa: E402
    PaidUsageStop,
    SafetyStop,
    UnsafeInput,
    guarded_send,
    init_problem,
)


def plugin_config(tmp_path, **kw):
    core = tmp_path / "core"
    core.mkdir(exist_ok=True)
    values = dict(workdir=tmp_path, config_dir=tmp_path, tools=("Read", "Skill"), permission_tool="mcp__pa__approve",
                  plugin_dirs=(core,), expected_plugins=("core",), expected_mcp=("pa",),
                  allowed_skills=("core:daily-plan",))
    values.update(kw)
    return BridgeConfig(**values)


def good_init(config, **extra):
    event = {"type": "system", "subtype": "init", "apiKeySource": "none", "tools": ["Read", "Skill"],  # pragma: allowlist secret
             "mcp_servers": [{"name": "pa", "status": "connected"}],
             "plugins": [{"name": "core", "path": str(config.plugin_dirs[0]), "source": "core@inline"},
                         {"name": "cc-plugin-telemetry", "path": "builtin", "source": "cc-plugin-telemetry@builtin"}],
             "skills": ["core:daily-plan", "dataviz", "cc-plugin-telemetry:x"]}
    event.update(extra)
    return event


class ConfiguredStub(StubSession):
    def __init__(self, name, config, events=()):
        super().__init__(name, events)
        self.config = config


def test_builtin_plugins_and_expected_init_pass(tmp_path):
    config = plugin_config(tmp_path)
    assert init_problem(good_init(config), config) is None
    main = ConfiguredStub("main", config, [good_init(config)])
    assert not make_router(main).handle("hello").refused


@pytest.mark.parametrize("change, expected", [
    ({"tools": ["Read", "Skill", "mcp__x__y"]}, "unexpected tool"),
    ({"tools": "Read"}, "does not list the tools"),
    ({"mcp_servers": [{"name": "pa"}, {"name": "claude.ai Mail"}]}, "unexpected MCP server"),
    ({"mcp_servers": ["pa"]}, "MCP server without a name"),
    ({"plugins": [{"name": "evil", "path": "/tmp/evil", "source": "evil@inline"}]}, "unexpected plugin"),
    ({"plugins": [{"name": "core", "path": "/elsewhere", "source": "core@inline"}]}, "unexpected path"),
    ({"plugins": [{"name": "core"}]}, "without a name or source"),
    ({"plugins": None}, "does not list the plugins"),
    ({"skills": ["evil:steal"]}, "unexpected plugin"),
])
def test_init_tripwire_cases(tmp_path, change, expected):
    config = plugin_config(tmp_path)
    event = good_init(config, **change)
    if change.get("plugins", 0) is None:
        del event["plugins"]
    assert expected in init_problem(event, config)


def test_small_session_must_list_no_tools(tmp_path):
    small = BridgeConfig(workdir=tmp_path, config_dir=tmp_path, tools=())
    assert init_problem({"type": "system", "subtype": "init"}, small) is None  # lists missing: not strict
    assert "unexpected tool" in init_problem({"tools": ["Read"]}, small)
    assert "unexpected plugin" in init_problem(
        {"plugins": [{"name": "core", "path": "/x", "source": "core@inline"}]}, small)


@pytest.mark.parametrize("event", [
    {"type": "system", "subtype": "hook_started", "hook_event": "SessionStart"},
    {"type": "system", "subtype": "hook_response"},
    {"type": "system", "subtype": "hook_progress"},
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "Skill", "input": {"skill": "update-config"}}]}},
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "Skill", "input": {}}]}},
])
def test_safety_stop_closes_session_and_halts(tmp_path, event):
    config = plugin_config(tmp_path)
    main = ConfiguredStub("main", config, [good_init(config), event])
    router = make_router(main)
    reply = router.handle("hello")
    assert reply.refused and reply.safety and reply.is_error and reply.text.startswith("Stopped:")
    assert main.closed == [0]
    later = router.handle("hello again")
    assert later.refused and later.safety and "Restart the chat" in later.text
    assert main.sent == ["hello"]


def test_allowed_skill_call_passes(tmp_path):
    config = plugin_config(tmp_path)
    call = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "Skill", "input": {"skill": "core:daily-plan"}}]}}
    main = ConfiguredStub("main", config, [good_init(config), call])
    assert not make_router(main).handle("plan my day").refused


def test_paid_usage_stop_is_a_safety_stop_but_does_not_halt():
    assert issubclass(PaidUsageStop, SafetyStop) and issubclass(UnsafeInput, SafetyStop)
    event = {"type": "rate_limit_event", "rate_limit_info": info(five=0.5, isUsingOverage=True, resetsAt=NOW + 3600)}
    router = make_router(StubSession("main", [event]))
    reply = router.handle("hello")
    assert reply.refused and not reply.safety and router.halted is None


def test_guarded_send_refuses_slash_before_starting(tmp_path):
    config = plugin_config(tmp_path)
    main = ConfiguredStub("main", config)
    for body in ("!/config", "/config", "​/clear", "/core", "/core:nonexistent", " /compact"):
        with pytest.raises(UnsafeInput):
            guarded_send(main, body, guard_with(None))
    guarded_send(main, "/core:daily-plan now", guard_with(None))
    assert main.sent == ["/core:daily-plan now"]


@pytest.mark.parametrize("text", ["!/config", "! /clear", "​/config", "/core", "/core:nonexistent"])
def test_router_refuses_slash_bypasses_at_tier1(tmp_path, text):
    config = plugin_config(tmp_path)
    main = ConfiguredStub("main", config)
    router = Router(main, None, guard_with(None), CFG, statuses=lambda: [],
                    slash_commands=frozenset({"/core:daily-plan"}))
    reply = router.handle(text)
    assert reply.tier == 1 and reply.is_error and main.sent == []


# --- end to end with the fake CLI and an enabled plugin -------------------------------------


def make_demo_plugin(root):
    files = {
        ".claude-plugin/plugin.json": '{"name": "demo", "version": "1.0.0"}',
        "skills/hello/SKILL.md": "---\nname: hello\ndescription: Greets back.\n---\nSay hello.\n",
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


@pytest.fixture
def plugin_chat(chat, tmp_path, monkeypatch):
    from personal_agent.config import Config as _Config
    from personal_agent.plugins import enable_plugin

    monkeypatch.setenv("PA_CORE_PLUGIN_DIR", "off")
    monkeypatch.delenv("FAKE_CLAUDE_EXTRA_INIT", raising=False)
    enable_plugin(_Config.from_env(), make_demo_plugin(tmp_path / "demo-src"))
    return chat


def test_chat_loads_enabled_plugin(plugin_chat, capsys, monkeypatch):
    monkeypatch.setattr("personal_agent.approval.stdin_is_tty", lambda: True)
    assert plugin_chat.run(["/demo:hello", "SKILL demo:hello", "/exit"]) == 0
    out = capsys.readouterr().out
    assert "[plugins: demo]" in out
    assert "agent[3]> echo: /demo:hello" in out
    assert "agent[3]> skill demo:hello loaded" in out and "[approval" not in out
    args = plugin_chat.starts()[0]["args"]
    assert args[args.index("--plugin-dir") + 1] == str(plugin_chat.state / "plugins" / "demo")
    assert "Skill" in args[args.index("--tools") + 1].split(",")


def test_chat_stops_on_builtin_skill_call(plugin_chat, capsys):
    assert plugin_chat.run(["SKILL update-config", "hello", "/exit"]) == 0
    out = capsys.readouterr().out
    assert "[safety] Stopped: the assistant tried to load a skill that is not allowed" in out
    assert "[safety] Not sent: the assistant was stopped earlier" in out


def test_chat_stops_on_hook_event(plugin_chat, capsys):
    assert plugin_chat.run(["HOOK", "/exit"]) == 0
    assert "[safety] Stopped: a hook ran inside the assistant session" in capsys.readouterr().out


@pytest.mark.parametrize("extra, message", [
    ('{"mcp_servers": [{"name": "pa"}, {"name": "intruder"}]}', "unexpected MCP server"),
    ('{"plugins": [{"name": "evil", "path": "/x", "source": "evil@inline"}]}', "unexpected plugin"),
    ('{"tools": ["Read", "mcp__x__y"]}', "unexpected tool"),
])
def test_chat_stops_on_unexpected_init(plugin_chat, capsys, extra, message):
    plugin_chat.make_wrapper(f"FAKE_CLAUDE_EXTRA_INIT={shlex.quote(extra)} ")
    assert plugin_chat.run(["hello", "/exit"]) == 0
    assert message in capsys.readouterr().out


REPO_CORE = FAKE.resolve().parents[1] / "plugins" / "core"


def test_chat_loads_the_shipped_core_plugin(chat, capsys, monkeypatch):
    monkeypatch.delenv("PA_CORE_PLUGIN_DIR", raising=False)
    monkeypatch.delenv("FAKE_CLAUDE_EXTRA_INIT", raising=False)
    monkeypatch.setattr("personal_agent.approval.stdin_is_tty", lambda: True)
    assert chat.run(["/core:daily-plan", "SKILL core:organize-inbox", "/core:nope", "/exit"]) == 0
    out = capsys.readouterr().out
    assert "[plugins: core]" in out and "core plugin not found" not in out
    assert "agent[3]> echo: /core:daily-plan" in out  # verbatim, so the CLI expands the skill
    assert "agent[3]> skill core:organize-inbox loaded" in out and "[approval" not in out
    assert "unknown command" in out and "[safety]" not in out
    starts = chat.starts()
    assert len(starts) == 1
    args = starts[0]["args"]
    assert [args[i + 1] for i, a in enumerate(args) if a == "--plugin-dir"] == [str(REPO_CORE)]
    assert "Skill" in args[args.index("--tools") + 1].split(",")


def test_chat_core_off_loads_no_plugin(chat, capsys, monkeypatch):
    monkeypatch.setenv("PA_CORE_PLUGIN_DIR", "off")
    assert chat.run(["hello", "/core:daily-plan", "/exit"]) == 0
    out = capsys.readouterr().out
    assert "[plugins:" not in out and "unknown command" in out
    assert "--plugin-dir" not in chat.starts()[0]["args"]


def test_chat_slash_bypass_is_refused_without_a_process(plugin_chat, capsys):
    assert plugin_chat.run(["!/config", "/demo", "/demo:nope", "/exit"]) == 0
    out = capsys.readouterr().out
    assert out.count("unknown command") == 3
    assert plugin_chat.starts() == []


# --- phase 3: task commands, the shared latch, quota thresholds for helpers ---------------

from personal_agent.router import SafetyLatch  # noqa: E402


class FakeTasks:
    def __init__(self):
        self.commands = []

    def command(self, command):
        self.commands.append(command)
        return f"handled {command.kind} {command.arg}", False


@pytest.mark.parametrize("text, kind", [
    ("task summarise the backups", "task_add"),
    ("tasks", "tasks"),
    ("task show 3f2a", "task_show"),
    ("task cancel 3f2a9c", "task_cancel"),
])
def test_task_commands_are_tier1_without_quota_or_session(text, kind):
    main, small = StubSession("main"), StubSession("small")
    tasks = FakeTasks()
    router = Router(main, small, guard_with(info(five=0.99, five_reset=NOW + 3600)), CFG, statuses=lambda: [],
                    tasks=tasks)
    router.halted = "a hook ran"  # task commands still work while halted
    reply = router.handle(text)
    assert reply.tier == 1 and not reply.refused and reply.text.startswith(f"handled {kind}")
    assert main.sent == [] and small.sent == []


def test_task_commands_when_tasks_are_off():
    router = make_router()
    reply = router.handle("task do something")
    assert reply.tier == 1 and reply.is_error and reply.text == "tasks are off (PA_TASKS=off)"
    assert router.handle("task").text.startswith("usage: task")


def test_force_prefix_sends_task_text_to_the_assistant():
    main = StubSession("main")
    tasks = FakeTasks()
    router = Router(main, None, guard_with(None), CFG, statuses=lambda: [], tasks=tasks)
    reply = router.handle("!task summarise this")
    assert reply.tier == 3 and main.sent == ["task summarise this"] and tasks.commands == []


def test_help_mentions_tasks_only_when_enabled():
    assert "task [@domain] <text>" not in make_router().handle("help").text
    router = Router(StubSession("main"), None, guard_with(None), CFG, statuses=lambda: [], tasks=FakeTasks())
    assert "task [@domain] <text>" in router.handle("help").text


def test_safety_latch_is_shared_and_first_reason_wins():
    latch = SafetyLatch()
    seen = []
    latch.add_listener(seen.append)
    router = Router(StubSession("main"), None, guard_with(None), CFG, statuses=lambda: [], latch=latch)
    assert router.halted is None and not latch
    latch.trip("helper: a hook ran\x07 inside")
    latch.trip("second reason")
    assert router.halted == "helper: a hook ran inside" and seen == ["helper: a hook ran inside"]
    assert router.handle("hello").refused
    late = []
    latch.add_listener(late.append)  # a late listener hears about an earlier trip
    assert late == ["helper: a hook ran inside"]


def test_quota_check_with_a_lower_threshold_and_freshness():
    clock = Clock()
    guard = guard_with(info(five=0.6, five_reset=NOW + 3600), clock=clock)
    assert not guard.check(stop_at=0.5).ok and "stop at 50 %" in guard.check(stop_at=0.5).reason
    assert guard.check().ok  # 0.9 for the chat
    assert guard.check(require_fresh_s=900).fresh
    clock.now += 901
    assert not guard.check(require_fresh_s=900).fresh and guard.check().fresh
    assert not guard_with(None).check(require_fresh_s=900).fresh


def test_chat_runs_a_task_and_shows_the_notice(chat, capsys):
    import json as _json

    state = chat.state
    done = {"id": None}

    def lines():
        yield "task hello from a helper"
        for _ in range(500):
            path = state / "tasks.json"
            if path.exists():
                tasks = _json.loads(path.read_text())["tasks"]
                if tasks and tasks[0]["status"] == "done":
                    done["id"] = tasks[0]["id"]
                    break
            time.sleep(0.02)
        yield "tasks"
        yield "/exit"

    from personal_agent.cli import main

    it = lines()
    import builtins

    original = builtins.input
    builtins.input = lambda prompt="": next(it)
    try:
        assert main(["chat"]) == 0
    finally:
        builtins.input = original
    out = capsys.readouterr().out
    task_id = done["id"]
    assert task_id, out
    assert f"agent[1]> task {task_id} queued (0 running, 0 ahead) · result: outputs/tasks/{task_id}.md" in out
    assert f"[task {task_id} done: outputs/tasks/{task_id}.md]" in out
    assert (state / "vault" / "outputs" / "tasks" / f"{task_id}.md").exists()
    assert f"{task_id}  done" in out
    helper_start = [s for s in chat.starts() if "--no-session-persistence" in s["args"]]
    assert len(helper_start) == 1


def test_chat_with_tasks_off(chat, capsys, monkeypatch):
    monkeypatch.setenv("PA_TASKS", "off")
    assert chat.run(["task x", "/exit"]) == 0
    assert "agent[1]> tasks are off (PA_TASKS=off)" in capsys.readouterr().out
    assert not (chat.state / "tasks.json").exists()
