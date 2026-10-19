import fcntl
import json
import os
import shlex
import stat
import sys
import time
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from personal_agent import brief as brief_mod
from personal_agent.bridge import BridgeConfig, ClaudeSession, TurnResult, build_args
from personal_agent.brief import (
    BriefScheduler,
    build_brief,
    collect_brief,
    discord_chunks,
    load_state,
    polish_brief,
    render_discord,
    run_brief,
    save_state,
    send_brief,
    validate_polish,
)
from personal_agent.config import Config
from personal_agent.quota import QuotaGuard
from personal_agent.remote import DiscordWebhook
from personal_agent.status import SessionStatus
from personal_agent.vault import Vault

FAKE = Path(__file__).resolve().parent / "fake_claude.py"
HOOK = "https://discord.com/api/webhooks/1/abcdef"
NOW = datetime(2026, 10, 18, 7, 30)
TS = NOW.timestamp()
T = TS  # scheduled time in the scheduler tests (brief_time 07:30)
WINDOWS = {"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.25},
                                                   "seven_day": {"utilization": 0.4}}}

GOLDEN_EN = (
    "# Brief 2026-10-18 (Sunday)\n\n## Focus (heute.md)\n### Top 3\n1. Ship the brief\n2.\n3.\n### Notes\n"
    "- [ ] call the bank\n\n## Open tasks\n- [ ] call the bank — heute.md\n- [ ] water the roses — wiki/garden.md\n\n"
    "## Claude Code sessions\n1 waiting, 1 working\n- waiting    10 min  shop                 needs input\n"
    "- working    20 min  blog                 writing tests\n\n## Quota\n"
    "five-hour window 25 % · seven-day window 40 % · status allowed · stop at 90 %\n\n## Inbox\n"
    "2 new captures since 06:00\n"
)
GOLDEN_DE = (
    "# Brief 2026-10-18 (Sonntag)\n\n## Fokus (heute.md)\n### Top 3\n1. Ship the brief\n2.\n3.\n### Notes\n"
    "- [ ] call the bank\n\n## Offene Aufgaben\n- [ ] call the bank — heute.md\n- [ ] water the roses — wiki/garden.md\n\n"
    "## Claude-Code-Sessions\n1 waiting, 1 working\n- waiting    10 min  shop                 needs input\n"
    "- working    20 min  blog                 writing tests\n\n## Kontingent\n"
    "five-hour window 25 % · seven-day window 40 % · status allowed · stop at 90 %\n\n## Eingang\n"
    "2 neue Notizen seit 06:00\n"
)


# --- helpers ---------------------------------------------------------------------------


class Response:
    def __init__(self, status=204):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def getcode(self):
        return self.status


class Opener:
    """Fake urllib opener for DiscordWebhook: records payloads, fails the listed post numbers."""

    def __init__(self, fail_at=(), always_fail=False):
        self.payloads = []
        self.fail_at = set(fail_at)
        self.always_fail = always_fail
        self.on_post = None

    def __call__(self, request, timeout):
        self.payloads.append(json.loads(request.data))
        if self.on_post is not None:
            self.on_post()
        if self.always_fail or len(self.payloads) in self.fail_at:
            raise urllib.error.URLError("down")
        return Response()

    @property
    def texts(self):
        return [p["content"] for p in self.payloads]


def webhook(opener):
    return DiscordWebhook(HOOK, opener=opener)


class StubSession:
    def __init__(self, text="", events=(), is_error=False):
        self.text = text
        self.events = list(events)
        self.is_error = is_error
        self.sent = []
        self.closed = []
        self.config = BridgeConfig(workdir=Path("/nonexistent"), config_dir=Path("/nonexistent"), tools=())

    def send(self, text, on_event=None):
        self.sent.append(text)
        for event in self.events:
            on_event(event)
        return TurnResult(self.text, "sid", self.is_error, "success", [], 0.0, None)

    def close(self, timeout_s=10):
        self.closed.append(timeout_s)


def make_config(tmp_path, **overrides):
    values = dict(
        state_dir=tmp_path / "state",
        status_dir=tmp_path / "sessions",
        vault_dir=tmp_path / "vault",
        brief_time=(7, 30),
        discord_webhook=HOOK,
        router_small_model=None,
        claude_config_dir=tmp_path / "claude-config",
    )
    values.update(overrides)
    return Config(**values)


def fill_vault(root: Path) -> Vault:
    vault = Vault(root, clock=lambda: TS)
    vault.init()
    (root / "heute.md").write_text(
        "# Today — 2026-10-18\n\n## Top 3\n\n1. Ship the brief\n2.\n3.\n\n## Notes\n- [ ] call the bank\n")
    (root / "wiki" / "garden.md").write_text("# Garden\n\n- [ ] water the roses\n- [x] prune\n")
    (root / "raw" / "inbox-2026-10-18.md").write_text(
        "# Inbox 2026-10-18\n\n- 06:10 first\n- 07:00 second\n- [ ] raw task\n")
    (root / "outputs" / "old.md").write_text("- [ ] ignored\n")
    os.utime(root / "wiki" / "garden.md", (TS - 3600, TS - 3600))
    return vault


def guard_with(info=None, stop=0.9):
    guard = QuotaGuard(stop, None, clock=lambda: TS)
    if info is not None:
        guard.observe(info)
    return guard


STATUSES = [
    SessionStatus("s1", "shop", "waiting", "needs input", TS - 60, TS - 600),
    SessionStatus("s2", "blog", "working", "writing tests", TS - 30, TS - 1200),
]
SINCE = datetime(2026, 10, 18, 6, 0).timestamp()


# --- deterministic content ----------------------------------------------------------------


def test_golden_brief_en_and_de(tmp_path):
    vault = fill_vault(tmp_path / "vault")
    guard = guard_with(WINDOWS)
    first = build_brief(vault, guard, STATUSES, NOW, SINCE, "en")
    assert first == GOLDEN_EN
    assert build_brief(vault, guard, STATUSES, NOW, SINCE, "en") == first  # byte-identical
    assert build_brief(Vault(vault.root, "de"), guard, STATUSES, NOW, SINCE, "de") == GOLDEN_DE


def test_raw_and_outputs_tasks_are_left_out(tmp_path):
    text = build_brief(fill_vault(tmp_path / "vault"), guard_with(), [], NOW, SINCE)
    assert "raw task" not in text and "ignored" not in text
    assert "no session status" in text and "no quota data yet" in text


def test_more_tasks_and_no_vault(tmp_path):
    vault = fill_vault(tmp_path / "vault")
    (vault.root / "wiki" / "todo.md").write_text("".join(f"- [ ] task {i}\n" for i in range(20)))
    text = build_brief(vault, guard_with(), [], NOW, SINCE)
    section = text.split("## Open tasks\n", 1)[1].split("\n\n", 1)[0]
    assert section.count("- [ ] ") == 15 and section.endswith("+7 more")
    for missing in (None, Vault(tmp_path / "nowhere")):
        text = build_brief(missing, guard_with(), [], NOW, SINCE)
        assert text.count("(no vault)") == 3 and "## Quota" in text


def test_capture_window_and_since_format(tmp_path):
    vault = fill_vault(tmp_path / "vault")
    text = build_brief(vault, guard_with(), [], NOW, TS - 30 * 86400)  # capped at 7 days back
    assert "2 new captures since 2026-10-11 07:30" in text


# --- Discord ----------------------------------------------------------------------------------


def test_discord_chunks_limits_and_truncation():
    lines = [f"line {i} " + "x" * 50 for i in range(200)]
    chunks = discord_chunks("\n".join(lines), max_chunks=100)
    assert all(len(c) <= 1900 for c in chunks) and len(chunks) > 1
    assert "\n".join(chunks).split("\n") == lines  # split only at line boundaries
    long = discord_chunks("a" * 4000 + "\nshort")
    assert [len(c) for c in long] == [1900, 1900, 206]  # hard split, then line packing again
    assert long[2] == "a" * 200 + "\nshort"
    many = discord_chunks("\n".join(lines), full_path="outputs/brief-2026-10-18.md")
    assert len(many) == 5 and len(many[-1]) <= 1900
    assert many[-1].endswith("… (truncated; full brief in outputs/brief-2026-10-18.md)")
    assert discord_chunks("a\x07b\u202ec\n\nd") == ["abc\n\nd"]


def test_send_brief_payloads_and_first_failure():
    opener = Opener()
    sleeps = []
    text = "\n".join("y" * 100 for _ in range(40))
    assert send_brief(webhook(opener), text, sleep=sleeps.append)
    assert len(opener.payloads) == 3 and sleeps == [0.6, 0.6]
    for payload in opener.payloads:
        assert len(payload["content"]) <= 1900
        assert payload["allowed_mentions"] == {"parse": []} and payload["flags"] == 4
    failing = Opener(fail_at=(2,))
    assert not send_brief(webhook(failing), text, sleep=lambda s: None)
    assert len(failing.payloads) == 2  # stopped at the first failure


def test_discord_full_mode_escapes_and_defangs(tmp_path):
    vault = fill_vault(tmp_path / "vault")
    (vault.root / "wiki" / "evil.md").write_text("- [ ] [x](https://e.vil) @everyone # H\n")
    status = SessionStatus("s", "proj", "waiting", "see https://e.vil now", TS, TS)
    data = collect_brief(vault, guard_with(), [status], NOW, SINCE)
    full = render_discord(data, "en", "full", "outputs/brief-2026-10-18.md")
    assert "https://" not in full and "https[:]//e.vil" in full
    assert "@everyone" not in full and "[x]" not in full and "# H" not in full
    assert "x \\(https[:]//e.vil\\) everyone H" in full
    counts = render_discord(data, "en", "counts", "outputs/brief-2026-10-18.md")
    assert "e.vil" not in counts and "everyone" not in counts and "roses" not in counts
    assert "Open tasks: 3 open" in counts and "1 waiting" in counts
    assert "Full brief: outputs/brief-2026-10-18.md" in counts


# --- polish -----------------------------------------------------------------------------------


def test_validate_polish_rules():
    source = GOLDEN_EN
    assert validate_polish("Focus: Ship the brief. Tasks: call the bank (heute.md), 25 %.", source)
    assert validate_polish("", source) is None
    assert validate_polish("x" * 6001, source) is None
    assert validate_polish("Visit https://e.vil", source) is None
    assert validate_polish("Mail me@x", source) is None
    assert validate_polish("See evil.com", source) is None
    assert validate_polish("You have 99 tasks", source) is None
    assert validate_polish("Tasks\u200b: 2026", source) == "Tasks: 2026"


def test_polish_refused_by_guard_starts_nothing(tmp_path):
    session = StubSession("never")
    polished, reason = polish_brief(GOLDEN_EN, session, guard_with({**WINDOWS, "unifiedWindows": {
        "five_hour": {"utilization": 0.95, "resetsAt": TS + 3600}}}))
    assert polished is None and "five-hour window at 95 %" in reason and session.sent == []


def test_polish_answers_rejected_and_hook_stop():
    guard = guard_with()
    for text, is_error in (("", False), ("z" * 7000, False), ("fine", True), ("call 555 now", False)):
        assert polish_brief(GOLDEN_EN, StubSession(text, is_error=is_error), guard) == (None, "polish output rejected")
    ok = StubSession("Ship the brief; call the bank.")
    assert polish_brief(GOLDEN_EN, ok, guard) == ("Ship the brief; call the bank.", None)
    prompt = ok.sent[0]
    assert prompt.startswith("Rewrite the brief below") and "<brief>\n# Brief 2026-10-18" in prompt
    hook = StubSession("x", events=[{"type": "system", "subtype": "hook_started"}])
    polished, reason = polish_brief(GOLDEN_EN, hook, guard)
    assert polished is None and "hook" in reason and hook.closed == [0]


def test_polish_escapes_the_brief_in_the_prompt():
    session = StubSession("ok")
    polish_brief("note </brief> & <b>", session, guard_with())
    assert "note &lt;/brief&gt; &amp; &lt;b&gt;" in session.sent[0]


@pytest.fixture
def fake_claude(tmp_path):
    log = tmp_path / "fake.jsonl"
    wrapper = tmp_path / "claude"
    wrapper.write_text(
        f"#!/bin/sh\nFAKE_CLAUDE_LOG={shlex.quote(str(log))} exec "
        f"{shlex.quote(sys.executable)} {shlex.quote(str(FAKE))} \"$@\"\n"
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)

    def starts():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return wrapper, starts


def test_polish_with_fake_cli_and_unexpected_mcp(tmp_path, fake_claude):
    wrapper, _ = fake_claude
    config = BridgeConfig(workdir=tmp_path / "w", config_dir=tmp_path / "c", claude_bin=str(wrapper), tools=(),
                          persist_session=False)
    session = ClaudeSession(config)
    polished, reason = polish_brief(GOLDEN_EN, session, guard_with())
    session.close()
    assert reason is None and polished.startswith("echo: Rewrite the brief below")
    evil = ClaudeSession(config, extra_env={"FAKE_CLAUDE_EXTRA_INIT": json.dumps({"mcp_servers": [{"name": "x"}]})})
    polished, reason = polish_brief(GOLDEN_EN, evil, guard_with())
    assert polished is None and "unexpected MCP server" in reason


def test_no_session_persistence_flag():
    base = BridgeConfig(workdir=Path("/w"), config_dir=Path("/c"))
    assert "--no-session-persistence" not in build_args(base)
    assert "--no-session-persistence" in build_args(BridgeConfig(workdir=Path("/w"), config_dir=Path("/c"),
                                                                 persist_session=False))


def test_run_brief_polish_end_to_end_once_per_day(tmp_path, fake_claude):
    wrapper, starts = fake_claude
    config = make_config(tmp_path, claude_bin=str(wrapper), router_small_model="haiku")
    fill_vault(config.vault_path)
    printed, notes = [], []
    failing = Opener(always_fail=True)
    assert run_brief(config, send=True, polish=True, now=NOW, webhook=webhook(failing), out=printed.append,
                     note=notes.append) == 1
    [start] = starts()
    args = start["args"]
    assert "--no-session-persistence" in args and "--plugin-dir" not in args and "--mcp-config" not in args
    assert args[args.index("--tools") + 1] == "" and args[args.index("--model") + 1] == "haiku"
    workdir = Path(start["cwd"])
    assert workdir.parent == config.state_dir / "run" and not workdir.exists()
    outputs = config.vault_path / "outputs"
    assert (outputs / "brief-2026-10-18.md").read_text().startswith("# Brief 2026-10-18")
    assert (outputs / "brief-2026-10-18.polished.md").read_text() == printed[0]
    assert printed[0].startswith("echo: Rewrite")
    # The retry reuses the polished file: three failed sends never mean three model calls.
    printed.clear()
    opener = Opener()
    assert run_brief(config, send=True, polish=True, now=NOW + timedelta(minutes=6), webhook=webhook(opener),
                     out=printed.append, note=notes.append) == 0
    assert len(starts()) == 1 and printed[0].startswith("echo: Rewrite") and len(opener.payloads) == 1
    # A later brief with other numbers does not reuse a polished text that no longer matches,
    # and does not ask the model again that day either.
    printed.clear()
    (config.vault_path / "wiki" / "new.md").write_text("- [ ] task 4711\n")
    assert run_brief(config, send=False, polish=True, now=NOW + timedelta(hours=2), out=printed.append,
                     note=notes.append) == 0
    assert len(starts()) == 1 and printed[0].startswith("# Brief")
    assert "does not match" in notes[-1]


def test_run_brief_polish_refusals(tmp_path):
    def boom():
        raise AssertionError("no session may start")

    config = make_config(tmp_path, router_small_model=None)
    fill_vault(config.vault_path)
    notes = []
    assert run_brief(config, False, True, now=NOW, session_factory=boom, note=notes.append) == 0
    assert notes == ["[quota] polish refused: PA_ROUTER_SMALL_MODEL is off"]
    # A rejected polish earlier today (no saved file) is not retried: one model call per day.
    with_model = make_config(tmp_path, router_small_model="haiku")
    save_state(with_model, {**brief_mod.DEFAULT_STATE, "polish_date": "2026-10-18"})
    notes.clear()
    assert run_brief(with_model, False, True, now=NOW, session_factory=boom, note=notes.append) == 0
    assert notes == ["[polish] already tried today: using the plain brief"]
    rejected = []
    assert run_brief(with_model, False, True, now=NOW + timedelta(days=1), note=rejected.append,
                     session_factory=lambda: StubSession("adds 12345")) == 0
    assert rejected == ["[polish] not used: polish output rejected"]
    assert load_state(with_model)[0]["polish_date"] == "2026-10-19"


def test_fresh_quota_guard_per_run_sees_external_updates(tmp_path):
    config = make_config(tmp_path, router_small_model="haiku")
    fill_vault(config.vault_path)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    quota = config.state_dir / "quota.json"
    now = time.time()
    quota.write_text(json.dumps({"observed_at": now, "status": "allowed",
                                 "windows": {"five_hour": {"utilization": 0.97, "resetsAt": now + 3600}}}))
    calls, notes, printed = [], [], []

    def factory():
        calls.append(1)
        return StubSession("Ship the brief")

    assert run_brief(config, False, True, now=NOW, session_factory=factory, note=notes.append,
                     out=printed.append) == 0
    assert calls == [] and notes[0].startswith("[quota] polish refused: five-hour window at 97 %")
    assert "blocked" in printed[0]
    quota.write_text(json.dumps({"observed_at": now, "status": "allowed",
                                 "windows": {"five_hour": {"utilization": 0.1}}}))
    assert run_brief(config, False, True, now=NOW, session_factory=factory, note=notes.append) == 0
    assert calls == [1]


def test_quota_guard_save_uses_a_unique_temp_file(tmp_path, monkeypatch):
    state = tmp_path / "quota.json"
    (tmp_path / "quota.tmp").write_text("someone else's temp file")
    guard = QuotaGuard(0.9, state)
    guard.observe(WINDOWS)
    assert (tmp_path / "quota.tmp").read_text() == "someone else's temp file"
    assert QuotaGuard(0.9, state).check().ok and json.loads(state.read_text())["status"] == "allowed"
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".quota-")] == []


# --- run_brief: files, state, lock ---------------------------------------------------------------


def test_run_brief_state_transitions(tmp_path):
    config = make_config(tmp_path)
    fill_vault(config.vault_path)
    opener = Opener()
    seen = []
    opener.on_post = lambda: seen.append(load_state(config)[0]["status"])
    assert run_brief(config, True, False, now=NOW, webhook=webhook(opener), note=lambda s: None) == 0
    assert seen == ["sending"]
    state, corrupt = load_state(config)
    assert not corrupt and state["status"] == "sent" and state["sent_by"] == "manual"
    assert state["date"] == "2026-10-18" and state["last_brief_at"] == TS and state["attempts"] == 0
    assert stat.S_IMODE((config.state_dir / "brief.json").stat().st_mode) == 0o600
    assert "Open tasks: 2 open" in opener.texts[0]  # counts mode by default
    assert "call the bank" not in opener.texts[0]

    failing = Opener(always_fail=True)
    later = NOW + timedelta(days=1)
    assert run_brief(config, True, False, now=later, webhook=webhook(failing), note=lambda s: None) == 1
    state = load_state(config)[0]
    assert state["status"] == "failed" and state["attempts"] == 1 and state["last_brief_at"] == TS
    assert run_brief(config, True, False, now=later + timedelta(minutes=6), webhook=webhook(failing),
                     note=lambda s: None) == 1
    assert load_state(config)[0]["attempts"] == 2


def test_run_brief_without_send_updates_since_only(tmp_path):
    config = make_config(tmp_path)
    fill_vault(config.vault_path)
    assert run_brief(config, False, False, now=NOW, note=lambda s: None) == 0
    state = load_state(config)[0]
    assert state["last_brief_at"] == TS and state["status"] is None


def test_send_without_webhook_exits_2(tmp_path):
    config = make_config(tmp_path, discord_webhook=None)
    notes = []
    assert run_brief(config, True, False, now=NOW, note=notes.append) == 2
    assert notes[0] == "[error] --send needs a brief channel (a paired Telegram bot or PA_DISCORD_WEBHOOK)"


def test_run_brief_honours_the_lock(tmp_path):
    config = make_config(tmp_path)
    fill_vault(config.vault_path)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(config.state_dir / "brief.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        notes = []
        opener = Opener()
        assert run_brief(config, True, False, now=NOW, webhook=webhook(opener), note=notes.append,
                         lock_wait_s=0.3) == 1
        assert notes == ["[error] another brief is running"] and opener.payloads == []
    finally:
        os.close(fd)


def test_untrusted_state_dir_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "state").symlink_to(real)
    config = make_config(tmp_path)
    notes = []
    assert run_brief(config, False, False, now=NOW, note=notes.append) == 1
    assert "symbolic link" in notes[0]


def test_corrupt_state_counts_as_empty(tmp_path):
    config = make_config(tmp_path)
    config.state_dir.mkdir(parents=True)
    for content in ("{", "[]", json.dumps({"version": 1, "status": "bogus"}),
                    json.dumps({"version": 1, "attempts": True}), "x" * 5000):
        (config.state_dir / "brief.json").write_text(content)
        assert load_state(config) == (None, True)
    (config.state_dir / "brief.json").unlink()
    assert load_state(config) == (None, False)


def test_outputs_symlink_still_sends(tmp_path):
    config = make_config(tmp_path)
    vault = fill_vault(config.vault_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    for child in (vault.root / "outputs").iterdir():
        child.unlink()
    (vault.root / "outputs").rmdir()
    (vault.root / "outputs").symlink_to(elsewhere)
    notes, opener = [], Opener()
    assert run_brief(config, True, False, now=NOW, webhook=webhook(opener), note=notes.append) == 0
    assert len(opener.payloads) == 1 and list(elsewhere.iterdir()) == []
    assert any("brief file not written" in n for n in notes)


def test_partial_failure_resumes_without_reposting(tmp_path):
    config = make_config(tmp_path, brief_discord="full")
    vault = fill_vault(config.vault_path)
    (vault.root / "wiki" / "long.md").write_text(
        "".join(f"- [ ] task {chr(97 + i)} " + "w" * 140 + "\n" for i in range(15)))
    opener = Opener(fail_at=(2,))
    assert run_brief(config, True, False, now=NOW, webhook=webhook(opener), note=lambda s: None,
                     sleep=lambda s: None) == 1
    state = load_state(config)[0]
    assert state["status"] == "failed" and state["targets"]["discord"]["chunks_sent"] == 1 and state["attempts"] == 1
    first = opener.texts[0]
    assert run_brief(config, True, False, now=NOW + timedelta(minutes=6), webhook=webhook(opener),
                     note=lambda s: None, sleep=lambda s: None) == 0
    assert opener.texts.count(first) == 1  # chunk 1 was not posted again
    assert opener.texts[2] == opener.texts[1] and len(opener.texts) == 3
    assert load_state(config)[0]["status"] == "sent"


def write_status(config, summary, state="working"):
    config.status_dir.mkdir(parents=True, exist_ok=True)
    (config.status_dir / "s1.json").write_text(json.dumps(
        {"session_id": "s1", "project": "shop", "state": state, "summary": summary,
         "updated_at": TS - 30, "since": TS - 600}))


def test_partial_failure_resumes_from_stored_chunks_when_live_data_changes(tmp_path):
    config = make_config(tmp_path, brief_discord="full")
    vault = fill_vault(config.vault_path)
    (vault.root / "wiki" / "long.md").write_text(
        "".join(f"- [ ] task {chr(97 + i)} " + "w" * 140 + "\n" for i in range(15)))
    write_status(config, "writing tests")
    opener = Opener(fail_at=(2,))
    assert run_brief(config, True, False, now=NOW, webhook=webhook(opener), note=lambda s: None,
                     sleep=lambda s: None) == 1
    stored = config.state_dir / "brief-chunks.json"
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    first, second = opener.texts[0], opener.texts[1]
    # Between the attempts the session status, the captures and the vault change.
    write_status(config, "reviewing the release", state="waiting")
    (vault.root / "raw" / "inbox-2026-10-18.md").write_text("# Inbox 2026-10-18\n\n- 07:31 new note\n")
    assert run_brief(config, True, False, now=NOW + timedelta(minutes=6), webhook=webhook(opener),
                     note=lambda s: None, sleep=lambda s: None) == 0
    assert opener.texts.count(first) == 1  # chunk 1 was not posted again
    assert opener.texts[2] == second  # the retry resends the stored chunk 2
    assert not any("reviewing the release" in t for t in opener.texts)
    assert load_state(config)[0]["status"] == "sent"
    assert not stored.exists()  # sent: the stored copy is removed


def test_stored_chunks_are_ignored_when_tampered_or_from_another_mode(tmp_path):
    config = make_config(tmp_path, brief_discord="full")
    fill_vault(config.vault_path)
    assert run_brief(config, True, False, now=NOW, webhook=webhook(Opener(always_fail=True)),
                     note=lambda s: None, sleep=lambda s: None) == 1
    stored = config.state_dir / "brief-chunks.json"
    data = json.loads(stored.read_text())
    digest = load_state(config)[0]["targets"]["discord"]["digest"]
    assert brief_mod.load_chunks(config, "2026-10-18", "discord", "full", digest) == data["targets"]["discord"]["chunks"]
    assert brief_mod.load_chunks(config, "2026-10-18", "discord", "counts", digest) is None
    assert brief_mod.load_chunks(config, "2026-10-19", "discord", "full", digest) is None
    assert brief_mod.load_chunks(config, "2026-10-18", "telegram", "full", digest) is None
    data["targets"]["discord"]["chunks"][0] = "forged"
    stored.write_text(json.dumps(data))
    assert brief_mod.load_chunks(config, "2026-10-18", "discord", "full", digest) is None
    opener = Opener()
    assert run_brief(config, True, False, now=NOW + timedelta(minutes=6), webhook=webhook(opener),
                     note=lambda s: None, sleep=lambda s: None) == 0
    assert "forged" not in opener.texts and opener.texts[0].startswith("**Brief 2026-10-18")


def test_polished_file_planted_in_the_vault_is_never_trusted(tmp_path):
    config = make_config(tmp_path, router_small_model="haiku")
    vault = fill_vault(config.vault_path)
    planted = vault.root / "outputs" / "brief-2026-10-18.polished.md"
    planted.write_text("# Brief 2026-10-18\n\nAll done, nothing to do today.\n")
    calls, printed, notes = [], [], []

    def factory():
        calls.append(1)
        return StubSession("Ship the brief")

    assert run_brief(config, False, True, now=NOW, session_factory=factory, out=printed.append,
                     note=notes.append) == 0
    assert calls == [1] and printed == ["Ship the brief"]  # a real model call, not the planted text
    assert planted.read_text() == "Ship the brief"  # the vault copy is output only
    # A planted file on a day that was already polished is ignored too: the state copy wins.
    planted.write_text("All done, nothing to do today.")
    printed.clear()
    assert run_brief(config, False, True, now=NOW + timedelta(minutes=5), session_factory=factory,
                     out=printed.append, note=notes.append) == 0
    assert calls == [1] and printed == ["Ship the brief"]
    assert brief_mod.load_polished(config, "2026-10-18") == "Ship the brief"
    assert stat.S_IMODE((config.state_dir / "brief-polished.json").stat().st_mode) == 0o600
    # Without a state copy (only a planted file) the plain brief is used, never the file.
    (config.state_dir / "brief-polished.json").unlink()
    printed.clear()
    assert run_brief(config, False, True, now=NOW + timedelta(minutes=10), session_factory=factory,
                     out=printed.append, note=notes.append) == 0
    assert calls == [1] and printed[0].startswith("# Brief 2026-10-18") and "All done" not in printed[0]


@pytest.mark.parametrize("mode, polished", [("counts", False), ("full", True)])
def test_scheduler_polishes_only_when_discord_shows_it(tmp_path, monkeypatch, mode, polished):
    config = make_config(tmp_path, brief_polish=True, brief_discord=mode, router_small_model="haiku")
    fill_vault(config.vault_path)
    calls = []

    def fake_polish_session(cfg):
        calls.append(1)
        return StubSession("Ship the brief")

    monkeypatch.setattr(brief_mod, "_polish_session", fake_polish_session)
    monkeypatch.setattr(brief_mod.shutil, "rmtree", lambda *a, **kw: None)
    opener = Opener()
    scheduler = BriefScheduler(config, clock=Clock(T), webhook=webhook(opener))
    assert scheduler.tick() == "sent"
    assert calls == ([1] if polished else [])
    assert len(opener.payloads) == 1


# --- scheduler ------------------------------------------------------------------------------------


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def sched(tmp_path):
    config = make_config(tmp_path)
    fill_vault(config.vault_path)
    opener = Opener()
    clock = Clock(T - 60)

    def make(cfg=None, op=None):
        return BriefScheduler(cfg or config, clock=clock, webhook=webhook(op or opener))

    return config, opener, clock, make


def test_scheduler_off(tmp_path):
    config = make_config(tmp_path, brief_time=None)
    assert BriefScheduler(config, run=lambda now: True).tick() == "off"


def test_scheduler_sends_once_and_survives_restart(sched):
    config, opener, clock, make = sched
    scheduler = make()
    assert scheduler.tick() == "not_due"
    clock.now = T
    assert scheduler.tick() == "sent"
    clock.now = T + 30
    assert scheduler.tick() == "already"
    assert make().tick() == "already"  # a restarted serve process
    clock.now = T - 10 + 20  # the clock jumps back a little after the send
    assert make().tick() == "already"
    assert len(opener.payloads) == 1
    state = load_state(config)[0]
    assert state["sent_by"] == "schedule" and state["status"] == "sent"


def test_scheduler_in_memory_guard_when_state_disappears(sched):
    config, opener, clock, make = sched
    scheduler = make()
    clock.now = T + 1
    assert scheduler.tick() == "sent"
    (config.state_dir / "brief.json").unlink()
    clock.now = T + 600
    assert scheduler.tick() == "already" and len(opener.payloads) == 1
    (config.state_dir / "brief.json").write_text("{corrupt")
    assert scheduler.tick() == "already" and len(opener.payloads) == 1


def test_scheduler_crash_while_sending_counts_as_sent(sched):
    config, opener, clock, make = sched
    config.state_dir.mkdir(parents=True, exist_ok=True)
    save_state(config, {**brief_mod.DEFAULT_STATE, "date": "2026-10-18", "status": "sending",
                        "last_attempt_at": T - 5, "sent_by": "schedule"})
    clock.now = T + 60
    assert make().tick() == "already" and opener.payloads == []


def test_scheduler_missed_without_catch_up(sched):
    config, opener, clock, make = sched
    config.state_dir.mkdir(parents=True, exist_ok=True)
    save_state(config, {**brief_mod.DEFAULT_STATE, "date": "2026-10-17", "status": "failed", "attempts": 1,
                        "last_attempt_at": T - 86400})
    clock.now = T + 120 * 60  # exactly at the end of the grace window
    assert make().tick() == "missed"
    clock.now = T + 86400 - 3600  # the next day, before its brief time: no catch-up for today/yesterday
    assert make().tick() == "not_due"
    assert opener.payloads == []


def test_scheduler_retries_spaced_and_gives_up(sched):
    config, _, clock, make = sched
    failing = Opener(always_fail=True)
    scheduler = make(op=failing)
    results = []
    for offset in (0, 30, 301, 330, 602, 903, 1300):
        clock.now = T + offset
        results.append(scheduler.tick())
    assert results == ["failed", "not_due", "failed", "not_due", "failed", "gave_up", "gave_up"]
    assert len(failing.payloads) == 3
    assert load_state(config)[0]["attempts"] == 3
    clock.now = T + 1400
    assert make(op=failing).tick() == "gave_up"  # a restart does not reset the limit


def test_scheduler_skips_after_manual_send(sched):
    config, opener, clock, make = sched
    assert run_brief(config, True, False, now=NOW - timedelta(hours=1), webhook=webhook(opener),
                     note=lambda s: None) == 0
    clock.now = T + 5
    assert make().tick() == "already" and len(opener.payloads) == 1


def test_scheduler_skips_a_locked_tick(sched):
    config, opener, clock, make = sched
    config.state_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(config.state_dir / "brief.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        clock.now = T
        assert make().tick() == "locked"
    finally:
        os.close(fd)
    assert make().tick() == "sent" and len(opener.payloads) == 1


def test_scheduler_passes_its_own_now(sched):
    config, _, clock, _ = sched
    seen = []
    clock.now = T + 7
    scheduler = BriefScheduler(config, run=lambda now: seen.append(now) or True, clock=clock)
    assert scheduler.tick() == "sent" and seen == [datetime.fromtimestamp(T + 7)]


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory modes")
def test_unwritable_state_never_posts_and_never_storms(sched):
    config, opener, clock, make = sched
    config.state_dir.mkdir(parents=True, exist_ok=True)
    (config.state_dir / "brief.lock").touch()
    config.state_dir.chmod(0o500)
    try:
        scheduler = make()
        results = []
        for offset in range(0, 150, 30):  # five ticks
            clock.now = T + offset
            results.append(scheduler.tick())
    finally:
        config.state_dir.chmod(0o700)
    assert opener.payloads == []
    assert results == ["failed", "not_due", "not_due", "not_due", "not_due"]


@pytest.fixture
def berlin():
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Berlin"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def test_scheduler_dst_gap(tmp_path, berlin):
    config = make_config(tmp_path, brief_time=(2, 30))  # 02:30 does not exist on 2026-03-29 in Berlin
    opener = Opener()
    clock = Clock(datetime(2026, 3, 29, 0, 30).timestamp())
    scheduler = BriefScheduler(config, clock=clock, webhook=webhook(opener))
    at = scheduler.scheduled_at(datetime(2026, 3, 29).date())
    assert datetime.fromtimestamp(at).date().isoformat() == "2026-03-29"
    assert scheduler.tick() == "not_due"
    results = []
    for offset in range(0, 7200, 600):
        clock.now = at + offset
        results.append(scheduler.tick())
    assert results.count("sent") == 1 and len(opener.payloads) == 1
