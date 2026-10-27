"""AP-T1: the notification layer (notify.py) and the migration of brief, task and approval notices."""

import ast
import inspect
import json
import logging
import socket
import stat
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import personal_agent
from personal_agent import brief as brief_mod
from personal_agent import notify
from personal_agent.bridge import NO_TELEMETRY_ENV, BridgeConfig, build_env
from personal_agent.brief import BriefScheduler, collect_brief, discord_chunks, load_state, run_brief
from personal_agent.config import Config, ConfigError
from personal_agent.coordinator import TaskEvent, TaskNotifier
from personal_agent.escalation import Event
from personal_agent.notify import (
    DiscordTarget,
    Notifier,
    NotifierPost,
    alert_notice,
    alert_outcome,
    info_notice,
    plain_line,
    render_approval_notice,
    resolve_route,
    split_message,
    task_counts_notice,
    task_detail_notice,
    utf16_len,
)
from personal_agent.quota import QuotaGuard
from personal_agent.remote import ApprovalInbox
from personal_agent.server import Agent
from personal_agent.telephony import FakeProvider
from personal_agent.vault import Vault

TOKEN = "123456789:" + "T" * 35  # synthetic, has the shape of a bot token
HOOK = "https://discord.com/api/webhooks/1/abcdef"
PUBLIC = "https://agent.tailnet.example"
NOW = datetime(2026, 10, 18, 7, 30)
TS = NOW.timestamp()
SRC = Path(personal_agent.__file__).resolve().parent
NOTIFY_ENV = ("PA_TELEGRAM_TOKEN", "PA_DISCORD_WEBHOOK", "PA_PUBLIC_APPROVAL_URL", "PA_TASK_DISCORD",
              "PA_BRIEF_TELEGRAM", "PA_NOTIFY_TASK_DETAIL", "PA_APPROVAL_PIN",
              *(f"PA_NOTIFY_{kind.upper()}" for kind in notify.KINDS))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """No test in this module may reach anything but loopback."""
    real = socket.socket.connect

    def guarded(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and host not in ("127.0.0.1", "::1", "localhost") and not host.startswith("/"):
            raise OSError("network access is not allowed in tests")
        return real(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


# --- fakes ---------------------------------------------------------------------


class Hook:
    """A Discord webhook stand-in: records posts, fails when `fail(text, n)` says so."""

    def __init__(self, fail=None, delay=0.0):
        self.posts = []
        self.fail = fail
        self.delay = delay

    def post(self, text):
        if self.delay:
            time.sleep(self.delay)
        self.posts.append(text)
        return not (self.fail and self.fail(text, len(self.posts)))


class FakeTelegram:
    """A Telegram channel stand-in with the real limits, unit and inert rule."""

    name = "telegram"
    limit = notify.TELEGRAM_LIMIT
    max_chunks = notify.TELEGRAM_MAX_CHUNKS

    def __init__(self, ready=True, fail_at=()):
        self.posts = []
        self.is_ready = ready
        self.fail_at = set(fail_at)

    def measure(self, text):
        return utf16_len(text)

    def inert(self, text, limit=160):
        return plain_line(text, limit)

    def ready(self):
        return None if self.is_ready else "telegram not paired"

    def post(self, text):
        self.posts.append(text)
        return len(self.posts) not in self.fail_at


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def cfg(tmp_path, **overrides):
    values = dict(state_dir=tmp_path / "state", status_dir=tmp_path / "sessions", vault_dir=tmp_path / "vault",
                  router_small_model=None, claude_config_dir=tmp_path / "claude-config")
    values.update(overrides)
    return Config(**values)


def routes(**kinds):
    return tuple((kind, tuple(value.split(","))) for kind, value in kinds.items())


# --- configuration -------------------------------------------------------------


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("PA_STATE_DIR", str(tmp_path / "state"))
    for name in NOTIFY_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_route_parsing(env):
    config = Config.from_env()
    assert config.notify_routes == () and config.route("brief") is None
    assert config.brief_telegram == "full" and config.notify_task_detail == "counts"
    env.setenv("PA_TELEGRAM_TOKEN", TOKEN)
    env.setenv("PA_DISCORD_WEBHOOK", HOOK)
    env.setenv("PA_NOTIFY_BRIEF", " Telegram, discord ,telegram ")
    env.setenv("PA_NOTIFY_ALERT", "none")
    config = Config.from_env()
    assert config.route("brief") == ("telegram", "discord")  # lowercased, de-duplicated, order kept
    assert config.route("alert") == ("none",) and config.route("info") is None
    for value, message in (("none,discord", "none cannot be combined"), ("mail", "comma-separated"),
                           (",", "comma-separated")):
        env.setenv("PA_NOTIFY_BRIEF", value)
        with pytest.raises(ConfigError, match=message):
            Config.from_env()
    env.delenv("PA_NOTIFY_BRIEF")
    env.setenv("PA_NOTIFY_APPROVAL", "telegram,discord")
    with pytest.raises(ConfigError, match="exactly one value"):
        Config.from_env()
    env.setenv("PA_NOTIFY_APPROVAL", "discord")
    with pytest.raises(ConfigError, match="PA_NOTIFY_APPROVAL=discord needs PA_PUBLIC_APPROVAL_URL"):
        Config.from_env()
    env.setenv("PA_PUBLIC_APPROVAL_URL", PUBLIC)
    assert Config.from_env().route("approval") == ("discord",)


def test_route_needs_its_channel_and_never_echoes_the_token(env):
    env.setenv("PA_NOTIFY_INFO", "telegram")
    with pytest.raises(ConfigError, match="PA_NOTIFY_INFO=telegram needs PA_TELEGRAM_TOKEN"):
        Config.from_env()
    env.setenv("PA_NOTIFY_INFO", "discord")
    with pytest.raises(ConfigError, match="PA_NOTIFY_INFO=discord needs PA_DISCORD_WEBHOOK"):
        Config.from_env()
    env.delenv("PA_NOTIFY_INFO")
    for bad in ("123:" + "S" * 40, "abcdef:" + "S" * 40, "123456789:short-SSSS", "123456789:" + "S" * 40 + "!"):
        env.setenv("PA_TELEGRAM_TOKEN", bad)
        with pytest.raises(ConfigError) as info:
            Config.from_env()
        assert "SSSS" not in str(info.value) and "PA_TELEGRAM_TOKEN" in str(info.value)
    env.setenv("PA_TELEGRAM_TOKEN", TOKEN)
    config = Config.from_env()
    assert config.telegram_token == TOKEN and TOKEN not in repr(config) and "T" * 35 not in repr(config)
    for name, bad in (("PA_BRIEF_TELEGRAM", "everything"), ("PA_NOTIFY_TASK_DETAIL", "results")):
        env.setenv(name, bad)
        with pytest.raises(ConfigError, match=name):
            Config.from_env()
        env.delenv(name)
    env.setenv("PA_BRIEF_TELEGRAM", "COUNTS")
    env.setenv("PA_NOTIFY_TASK_DETAIL", "title")
    config = Config.from_env()
    assert config.brief_telegram == "counts" and config.notify_task_detail == "title"


def test_the_bot_token_never_reaches_the_claude_process():
    base = {"PATH": "/usr/bin", "HOME": "/home/x", "PA_TELEGRAM_TOKEN": TOKEN, "PA_DISCORD_WEBHOOK": HOOK}
    child = build_env(BridgeConfig(workdir=Path("/w"), config_dir=Path("/c")), base=base)
    assert TOKEN not in json.dumps(child) and HOOK not in json.dumps(child)
    assert set(child) <= {"PATH", "HOME", "LANG", "LC_ALL", "TZ", "CLAUDE_CONFIG_DIR", *NO_TELEMETRY_ENV}


# --- route resolution ----------------------------------------------------------


@pytest.mark.parametrize("kind, telegram, webhook, public, task_discord, expected", [
    ("brief", True, True, True, "off", ("telegram",)),
    ("brief", False, True, False, "off", ("discord",)),
    ("brief", False, False, False, "off", ()),
    ("approval", True, True, True, "off", ("telegram",)),
    ("approval", False, True, True, "off", ("discord",)),
    ("approval", False, True, False, "off", ()),       # webhook alone: no approval page
    ("task_done", True, False, False, "off", ("telegram",)),
    ("task_done", False, True, False, "off", ()),      # Discord stays opt-in (PA_TASK_DISCORD)
    ("task_done", False, True, False, "counts", ("discord",)),
    ("task_done", False, False, False, "counts", ()),
    ("alert", True, True, True, "counts", ("telegram",)),
    ("alert", False, True, True, "counts", ()),        # opt-in on Discord
    ("info", False, True, True, "counts", ()),
    ("info", True, False, False, "off", ("telegram",)),
])
def test_default_resolution_matrix(tmp_path, kind, telegram, webhook, public, task_discord, expected):
    config = cfg(tmp_path, discord_webhook=HOOK if webhook else None,
                 public_approval_url=PUBLIC if public else None, task_discord=task_discord)
    assert resolve_route(config, kind, telegram) == expected


def test_explicit_routes_win_and_none_means_nothing(tmp_path):
    config = cfg(tmp_path, discord_webhook=HOOK, telegram_token=TOKEN,
                 notify_routes=routes(alert="discord", brief="none", info="telegram,discord"))
    assert resolve_route(config, "alert", True) == ("discord",)
    assert resolve_route(config, "brief", True) == ()
    assert resolve_route(config, "info", False) == ("telegram", "discord")
    with pytest.raises(ValueError):
        resolve_route(config, "sms", False)


def test_pairing_at_runtime_and_no_fallback_for_an_explicit_channel(tmp_path, caplog):
    tg, hook = FakeTelegram(ready=False), Hook()
    config = cfg(tmp_path, discord_webhook=HOOK, telegram_token=TOKEN)
    clock = Clock()
    notifier = Notifier(config, {"discord": DiscordTarget(hook), "telegram": tg}, clock=clock, run_thread=False)
    assert [t.name for t in notifier.targets_for("brief")] == ["discord"]
    tg.is_ready = True  # the owner paired while the process runs: no restart needed
    assert [t.name for t in notifier.targets_for("brief")] == ["telegram"]

    explicit = cfg(tmp_path, discord_webhook=HOOK, telegram_token=TOKEN, notify_routes=routes(alert="telegram"))
    tg.is_ready = False
    notifier = Notifier(explicit, {"discord": DiscordTarget(hook), "telegram": tg}, clock=clock, run_thread=False)
    with caplog.at_level(logging.WARNING, logger="personal_agent.notify"):
        assert notifier.targets_for("alert") == []      # no silent fallback to Discord
        assert notifier.targets_for("alert") == []
        assert caplog.text.count("telegram not paired") == 1
        clock.now += 601
        notifier.targets_for("alert")
        assert caplog.text.count("telegram not paired") == 2
    assert notifier.unready_reasons("alert") == ["telegram not paired"]
    without = Notifier(explicit, {"discord": DiscordTarget(hook)}, run_thread=False)
    assert without.targets_for("alert") == [] and without.unready_reasons("alert") == ["telegram channel not available"]


# --- chunking per channel ------------------------------------------------------


def test_split_message_lines_units_and_overflow():
    lines = [f"line {i} " + "x" * 50 for i in range(200)]
    chunks = split_message("\n".join(lines), 1900, 100)
    assert all(len(c) <= 1900 for c in chunks) and "\n".join(chunks).split("\n") == lines
    # UTF-16: 2000 emoji are 2000 characters but 4000 units.
    assert split_message("😀" * 2000, 4000, 5, utf16_len) == ["😀" * 2000]
    assert len(split_message("😀" * 2001, 4000, 5, utf16_len)) == 2
    assert split_message("x" * 4000, 4000, 5, len) == ["x" * 4000]
    hard = split_message("a" + "😀" * 3000, 4000, 5, utf16_len)
    assert hard[0] == "a" + "😀" * 1999  # never cuts inside a surrogate pair
    assert "".join(hard) == "a" + "😀" * 3000 and all(utf16_len(c) <= 4000 for c in hard)
    for chunk in hard:
        chunk.encode("utf-8")  # strict: no lone surrogate
    many = split_message("\n".join("😀" * 1000 for _ in range(30)), 4000, 5, utf16_len, "\n… (cut)")
    assert len(many) == 5 and many[-1].endswith("\n… (cut)") and utf16_len(many[-1]) <= 4000
    assert split_message("a\x07b‮c\n\nd", 100, 5) == ["abc\n\nd"]


def test_split_message_can_keep_tabs():
    # Default (Discord brief): tabs go, like every control character except newlines.
    assert split_message("a\tb\nc", 100, 5) == ["ab\nc"]
    # Model replies over Telegram keep them: "a\tb" must not arrive as "ab".
    assert split_message("a\tb\n\tc\x07", 100, 5, keep="\n\t") == ["a\tb\n\tc"]
    assert split_message("a\tb", 100, 5, keep="\t") == ["a\tb"]  # newlines are always kept


def test_discord_chunks_are_the_generic_split():
    text = "\n".join(f"row {i} " + "y" * 90 for i in range(300))
    note = "\n… (truncated; full brief in outputs/x.md)"
    assert discord_chunks(text, full_path="outputs/x.md") == split_message(text, 1900, 5, len, note)
    assert discord_chunks("a" * 4000 + "\nshort") == ["a" * 1900, "a" * 1900, "a" * 200 + "\nshort"]


def test_notifier_splits_per_channel(tmp_path):
    hook, tg = Hook(), FakeTelegram()
    config = cfg(tmp_path, discord_webhook=HOOK, telegram_token=TOKEN, notify_routes=routes(info="telegram,discord"))
    notifier = Notifier(config, {"discord": DiscordTarget(hook), "telegram": tg}, run_thread=False,
                        sleep=lambda s: None)
    long_text = "\n".join("z" * 1000 for _ in range(7))  # seven lines of 1000 characters
    assert notifier.send_now("info", lambda target: long_text)
    assert [utf16_len(p) for p in tg.posts] == [3002, 3002, 1000]  # three lines fit in 4000 units
    assert len(hook.posts) == 5 and all(len(p) <= 1900 for p in hook.posts)  # one line each, cut at 5
    assert hook.posts[-1].endswith("\n… (cut)")


# --- privacy by construction ---------------------------------------------------


def _calls(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            yield node


def test_every_notice_send_passes_a_notify_renderer():
    seen = 0
    for path in SRC.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in _calls(tree):
            if call.func.attr not in ("send", "send_now") or not call.args:
                continue
            first = call.args[0]
            if not (isinstance(first, ast.Constant) and first.value in notify.KINDS):
                continue
            seen += 1
            build = call.args[1]
            assert isinstance(build, ast.Call), f"{path.name}:{call.lineno} passes no renderer"
            name = build.func.id if isinstance(build.func, ast.Name) else getattr(build.func, "attr", "")
            assert name in notify.NOTICE_FACTORIES, f"{path.name}:{call.lineno} uses {name}"
            if call.func.attr == "send":
                assert first.value in notify.ASYNC_KINDS
        for call in ast.walk(tree):
            if isinstance(call, ast.Call) and getattr(call.func, "id", None) == "NotifierPost":
                assert len(call.args) == 2 and getattr(call.args[1], "value", None) == "task_done", path.name
    assert seen >= 2  # the alert in server.py and the brief info notice
    source = inspect.getsource(TaskNotifier)
    assert "task_counts_notice(" in source and "task_detail_notice(" in source


def test_approval_notice_has_no_input_parameter_and_discord_is_unchanged():
    params = set(inspect.signature(render_approval_notice).parameters)
    assert params == {"target_name", "tool_name", "level", "origin_label", "context", "minutes", "link", "language",
                      "sequence"}
    link = f"{PUBLIC}/approve/{'a' * 32}"
    assert render_approval_notice("discord", "mcp__x__a*b", 2, "chat", True, 5, link) == (
        f"Approval needed (level 2): mcp\\_\\_x\\_\\_a b. Open <{link}> (expires in 5 min).")
    assert render_approval_notice("discord", "Write", 3, "chat", False, 1, link, "de") == (
        f"Freigabe nötig (Stufe 3): Write. Öffnen: <{link}> (läuft in 1 Min. ab).")
    with pytest.raises(ValueError):
        render_approval_notice("discord", "Write", 2, "chat", False, 5, None)
    plain = render_approval_notice("telegram", "Write", 2, "task 3f2a9c (research)", True, 4, None)
    assert plain == ("Approval needed · task 3f2a9c (research)\nLevel 2 (write): Write\n"
                     "(this request may come from note text or a plugin skill)\nExpires in 4 min.")
    with_link = render_approval_notice("telegram", "Write", 3, "chat", False, 4, link)
    assert with_link.endswith(f"Level 3 (danger): Write\nExpires in 4 min.\nReview: {link}")


def test_task_notices_counts_and_titles():
    assert task_counts_notice(1, 2, 3, 4, 5)(None) == "Tasks: 1 done, 2 failed, 3 cancelled · 4 running, 5 queued"
    params = set(inspect.signature(task_detail_notice).parameters)
    assert params == {"task_id", "status", "title"}  # no result, error, domain or path
    tg = FakeTelegram()
    line = task_detail_notice("3f2a9c", "done", "Read https://e.vil " + "x" * 100)(tg)
    assert line.startswith("- task 3f2a9c done: Read https[:]//e.vil") and len(line) < 100
    assert task_detail_notice("../etc", "exploded", None)(tg) == "- task - -"


def test_alert_and_info_texts_are_inert():
    event = Event(key="disk_*full*", severity="critical", summary="see https://e.vil @everyone", source="cli")
    outcome = alert_outcome(False, "quiet hours")
    discord = alert_notice(event, outcome)(DiscordTarget(Hook()))
    assert discord.startswith("Alert (critical) disk\\_ full")
    assert "https[:]//e.vil" in discord and "@" not in discord and discord.endswith("— no call: quiet hours")
    plain = alert_notice(event, alert_outcome(True, "x"))(FakeTelegram())
    assert plain == "Alert (critical) disk_*full*: see https[:]//e.vil @everyone — calling you now"
    assert info_notice("brief_gave_up")(FakeTelegram()).startswith("The morning brief could not be sent today")
    with pytest.raises(ValueError):
        info_notice("anything else")


# --- notifier: async delivery and kind limits ----------------------------------


def test_alert_dedupe_and_rate_limit(tmp_path):
    hook = Hook()
    clock = Clock()
    config = cfg(tmp_path, discord_webhook=HOOK, notify_routes=routes(alert="discord", info="discord"))
    notifier = Notifier(config, {"discord": DiscordTarget(hook)}, clock=clock, run_thread=False)
    event = Event("waiting:s1", "critical", "shop waits")
    for _ in range(3):  # the session watch escalates the same key every minute
        notifier.send("alert", alert_notice(event, "calling you now"), key=event.key)
        clock.now += 60
    assert len(hook.posts) == 1
    clock.now += 3600
    notifier.send("alert", alert_notice(event, "calling you now"), key=event.key)
    assert len(hook.posts) == 2
    for i in range(12):
        notifier.send("alert", alert_notice(Event(f"k{i}", "warning", "x"), "no call"), key=f"k{i}")
    assert len(hook.posts) == 2 + 9  # at most 10 alerts per 10 minutes
    clock.now += 601
    notifier.send("info", info_notice("brief_gave_up"), key="brief_gave_up")
    notifier.send("info", info_notice("brief_gave_up"), key="brief_gave_up")
    assert len(hook.posts) == 12
    with pytest.raises(ValueError):
        notifier.send("brief", info_notice("brief_gave_up"))


def test_async_queue_overflow_and_close_flushes(tmp_path, caplog):
    release = threading.Event()

    class Slow(Hook):
        def post(self, text):
            release.wait(10)
            return super().post(text)

    hook = Slow()
    config = cfg(tmp_path, discord_webhook=HOOK, task_discord="counts")
    notifier = Notifier(config, {"discord": DiscordTarget(hook)})
    build = task_counts_notice(1, 0, 0, 0, 0)
    with caplog.at_level(logging.WARNING, logger="personal_agent.notify"):
        notifier.send("task_done", build)
        deadline = time.monotonic() + 5
        while not notifier._queue.empty() and time.monotonic() < deadline:  # the thread took the first one
            time.sleep(0.01)
        for _ in range(notify.QUEUE_SIZE + 3):
            notifier.send("task_done", build)
    assert caplog.text.count("dropped (queue full)") == 1
    release.set()
    notifier.close(5.0)
    assert len(hook.posts) == 1 + notify.QUEUE_SIZE
    assert notifier._thread is not None and not notifier._thread.is_alive()
    assert "Tasks:" not in caplog.text  # never the text


def test_send_starts_no_thread_when_nothing_is_routed(tmp_path):
    notifier = Notifier(cfg(tmp_path), {})
    notifier.send("alert", info_notice("brief_gave_up"))
    assert notifier._thread is None
    notifier.close()


# --- migration: task notices ---------------------------------------------------


def test_task_notifier_through_the_layer_keeps_the_discord_text(tmp_path):
    hook = Hook()
    config = cfg(tmp_path, discord_webhook=HOOK, task_discord="counts")
    messenger = Notifier(config, {"discord": DiscordTarget(hook)}, run_thread=False)
    notifier = TaskNotifier(NotifierPost(messenger, "task_done"), None, run_thread=False)
    notifier.record(TaskEvent("abc123", "done", "outputs/tasks/abc123.md", None, title="Secret Title"))
    assert notifier.flush()
    assert hook.posts == ["Tasks: 1 done, 0 failed, 0 cancelled · 0 running, 0 queued"]


def test_task_detail_titles_are_inert_per_channel(tmp_path):
    hook, tg = Hook(), FakeTelegram()
    config = cfg(tmp_path, discord_webhook=HOOK, telegram_token=TOKEN,
                 notify_routes=routes(task_done="telegram,discord"))
    messenger = Notifier(config, {"discord": DiscordTarget(hook), "telegram": tg}, run_thread=False)
    notifier = TaskNotifier(NotifierPost(messenger), None, run_thread=False, detail="title")
    notifier.record(TaskEvent("abc123", "done", "outputs/tasks/abc123.md", None,
                              title="Pay *bank* https://e.vil @everyone"))
    notifier.record(TaskEvent("bbbbbb", "failed", None, "Claude Code failed: secret error", title="Second"))
    assert notifier.flush()
    for text in (hook.posts[0], tg.posts[0]):
        assert text.startswith("Tasks: 1 done, 1 failed, 0 cancelled")
        assert "abc123 done" in text and "bbbbbb failed: Second" in text
        assert "outputs" not in text and "secret error" not in text and "https://" not in text
    assert "@" not in hook.posts[0] and "\\*" not in hook.posts[0]
    assert "Pay *bank* https[:]//e.vil @everyone" in tg.posts[0]
    plain = Hook()
    counts_only = TaskNotifier(plain, None, run_thread=False)  # a plain webhook, counts mode
    counts_only.record(TaskEvent("abc123", "done", None, None, title="Secret Title"))
    assert counts_only.flush() and "abc123" not in plain.posts[0] and "Secret" not in plain.posts[0]


# --- migration: approval notices -----------------------------------------------


class FakeNotifier:
    def __init__(self):
        self.posts = []

    def post(self, text):
        self.posts.append(text)
        return True


def test_approval_inbox_follows_the_route():
    for route, works in ((lambda: "discord", True), (lambda: None, False), (lambda: "telegram", False)):
        fake = FakeNotifier()
        inbox = ApprovalInbox(fake, PUBLIC, route=route)
        entry = inbox.register("Write", 2, "SECRET input", 120)
        assert (entry is not None) is works and len(fake.posts) == int(works)
    assert ApprovalInbox(None, None).register("Write", 2, "x", 120) is None


def test_agent_approval_route_from_config(tmp_path):
    base = dict(api_token="t" * 32, call_to="+447700900123", call_pin="482913", discord_webhook=HOOK,
                public_approval_url=PUBLIC, approval_pin="731946")
    agent = Agent(cfg(tmp_path, **base), provider=FakeProvider())
    assert agent.approval_route() == "discord"
    fake = FakeNotifier()
    agent.approvals.notifier = fake
    assert agent.approvals.register("Write", 2, "x", 120) is not None
    off = Agent(cfg(tmp_path, notify_routes=routes(approval="none"), **base), provider=FakeProvider())
    assert off.approval_route() is None
    off.approvals.notifier = fake
    assert off.approvals.register("Write", 2, "x", 120) is None and len(fake.posts) == 1


# --- alerts from serve ---------------------------------------------------------


def server_cfg(tmp_path, **kw):
    return cfg(tmp_path, api_token="t" * 32, call_to="+447700900123", call_pin="482913", min_call_interval_min=0,
               discord_webhook=HOOK, **kw)


def test_escalate_sends_an_async_alert_deduplicated_per_key(tmp_path):
    hook = Hook(delay=1.0)  # a slow channel must not delay /escalate
    config = server_cfg(tmp_path, notify_routes=routes(alert="discord"))
    notifier = Notifier(config, {"discord": DiscordTarget(hook)})
    agent = Agent(config, provider=FakeProvider(), notifier=notifier)
    started = time.monotonic()
    assert agent.escalate(Event("disk", "critical", "Disk full, see https://e.vil"))["called"] is True
    agent.escalate(Event("disk", "critical", "Disk full, see https://e.vil"))
    assert time.monotonic() - started < 0.8
    notifier.close(5.0)
    assert len(hook.posts) == 1
    assert hook.posts[0] == "Alert (critical) disk: Disk full, see https[:]//e.vil — calling you now"


def test_alerts_are_opt_in_on_discord(tmp_path):
    hook = Hook()
    config = server_cfg(tmp_path)
    notifier = Notifier(config, {"discord": DiscordTarget(hook)}, run_thread=False)
    Agent(config, provider=FakeProvider(), notifier=notifier).escalate(Event("disk", "critical", "Disk full"))
    assert hook.posts == []


# --- migration: the brief ------------------------------------------------------


def fill_vault(root: Path, long: bool = False) -> Vault:
    vault = Vault(root, clock=lambda: TS)
    vault.init()
    focus = "".join(f"- line {i} https://e.vil/{i} " + "f" * 150 + "\n" for i in range(40))
    (root / "heute.md").write_text("# Today\n\n" + focus)
    (root / "wiki" / "garden.md").write_text("# Garden\n\n- [ ] water the **roses**\n")
    if long:  # emoji count twice in Telegram's unit: the full brief needs two messages
        (root / "wiki" / "long.md").write_text("".join(f"- [ ] task {i} " + "😀" * 150 + "\n" for i in range(15)))
    return vault


def test_render_plain_has_no_markup_and_defangs(tmp_path):
    vault = fill_vault(tmp_path / "vault")
    data = collect_brief(vault, QuotaGuard(0.9, None, clock=lambda: TS), [], NOW, TS - 3600)
    tg = FakeTelegram()
    full = brief_mod.render_for_target(tg, data, "en", "full", "outputs/brief-2026-10-18.md")
    assert full.startswith("Brief 2026-10-18 (Sunday)\n\nFocus (heute.md)\n")
    assert "https://" not in full and "https[:]//e.vil/3" in full and "water the **roses**" in full
    assert "Full brief: outputs/brief-2026-10-18.md" in full
    counts = brief_mod.render_for_target(tg, data, "en", "counts", "outputs/x.md")
    assert "roses" not in counts and "e.vil" not in counts and "Open tasks: 1 open" in counts
    assert brief_mod.render_for_target(DiscordTarget(Hook()), data, "en", "counts", "outputs/x.md").startswith("**Brief")


def test_telegram_brief_shows_session_counts_only(tmp_path):
    from personal_agent.status import SessionStatus

    vault = fill_vault(tmp_path / "vault")
    statuses = [SessionStatus("s1", "secretproj", "waiting", "approve the payroll export", TS - 30, TS - 600)]
    data = collect_brief(vault, QuotaGuard(0.9, None, clock=lambda: TS), statuses, NOW, TS - 3600)
    for mode in ("full", "counts"):
        text = brief_mod.render_for_target(FakeTelegram(), data, "en", mode, "outputs/x.md")
        assert "1 waiting" in text
        assert "secretproj" not in text and "payroll" not in text  # session lists stay on this machine
    # The deterministic Discord brief (opt-in full mode) is unchanged; the vault copy keeps everything.
    assert "secretproj" in brief_mod.render_for_target(DiscordTarget(Hook()), data, "en", "full", "outputs/x.md")
    assert "secretproj" in brief_mod.render_markdown(data)


def test_polish_input_leaves_session_lines_out(tmp_path, monkeypatch):
    config = cfg(tmp_path, telegram_token=TOKEN, brief_polish=True, router_small_model="haiku")
    fill_vault(config.vault_path)
    config.status_dir.mkdir(parents=True, exist_ok=True)
    (config.status_dir / "s1.json").write_text(json.dumps(
        {"session_id": "s1", "project": "secretproj", "state": "waiting", "summary": "approve payroll",
         "updated_at": TS - 30, "since": TS - 600}))
    sent = []

    class Stub:
        config = BridgeConfig(workdir=Path("/nonexistent"), config_dir=Path("/nonexistent"), tools=())

        def send(self, text, on_event=None):
            sent.append(text)
            from personal_agent.bridge import TurnResult
            return TurnResult("Ship it.", "sid", False, "success", [], 0.0, None)

        def close(self, timeout_s=10):
            pass

    monkeypatch.setattr(brief_mod.shutil, "rmtree", lambda *a, **kw: None)
    printed = []
    assert run_brief(config, False, True, now=NOW, out=printed.append, session_factory=Stub,
                     note=lambda s: None) == 0
    assert len(sent) == 1 and "1 waiting" in sent[0]
    assert "secretproj" not in sent[0] and "payroll" not in sent[0]
    assert printed == ["Ship it."]


def test_brief_to_two_channels_resumes_only_the_failed_one(tmp_path):
    config = cfg(tmp_path, discord_webhook=HOOK, telegram_token=TOKEN, notify_routes=routes(brief="telegram,discord"))
    vault = fill_vault(config.vault_path, long=True)
    hook, tg = Hook(), FakeTelegram(fail_at=(2,))
    notes = []
    assert run_brief(config, True, False, now=NOW, targets=[DiscordTarget(hook), tg], note=notes.append,
                     sleep=lambda s: None) == 1
    state = load_state(config)[0]
    assert state["status"] == "failed" and state["attempts"] == 1
    assert state["targets"]["discord"]["done"] is True and state["targets"]["telegram"]["chunks_sent"] == 1
    assert notes[-1] == "[error] brief send failed at chunk 2/2 (telegram)"
    assert len(hook.posts) == 1 and hook.posts[0].startswith("**Brief") and "Open tasks: 16 open" in hook.posts[0]
    stored = config.state_dir / "brief-chunks.json"
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    first, second = tg.posts
    (vault.root / "wiki" / "new.md").write_text("- [ ] a new task\n")  # live data changes in between
    hook2, tg2 = Hook(), FakeTelegram()
    assert run_brief(config, True, False, now=NOW + timedelta(minutes=6), targets=[DiscordTarget(hook2), tg2],
                     note=notes.append, sleep=lambda s: None) == 0
    assert hook2.posts == []          # Discord got everything the first time
    assert tg2.posts == [second]      # only the missing Telegram chunk, from the stored copy
    assert first.startswith("Brief 2026-10-18 (Sunday)\n\nFocus (heute.md)\n") and "**Open tasks" not in first
    state = load_state(config)[0]
    assert state["status"] == "sent" and state["targets"]["telegram"]["done"] is True
    assert not stored.exists()


def test_retry_counts_an_unready_recorded_channel_as_failed(tmp_path):
    config = cfg(tmp_path, discord_webhook=HOOK, telegram_token=TOKEN)
    fill_vault(config.vault_path)
    assert run_brief(config, True, False, now=NOW, targets=[FakeTelegram(fail_at=(1,))], note=lambda s: None,
                     sleep=lambda s: None) == 1
    hook, notes = Hook(), []
    # Telegram is gone (unpaired); a newly ready Discord is not added mid-day.
    assert run_brief(config, True, False, now=NOW + timedelta(minutes=6), targets=[DiscordTarget(hook)],
                     note=notes.append, sleep=lambda s: None) == 1
    assert hook.posts == [] and "[error] brief not sent to telegram: the channel is not ready" in notes
    assert load_state(config)[0]["attempts"] == 2


def test_v1_state_and_chunks_migrate(tmp_path):
    config = cfg(tmp_path, discord_webhook=HOOK)
    fill_vault(config.vault_path)
    config.state_dir.mkdir(parents=True, mode=0o700)
    chunks = ["**Brief 2026-10-18 (Sunday)** part one", "part two"]
    digest = brief_mod._chunks_digest(chunks)
    (config.state_dir / "brief.json").write_text(json.dumps({
        "version": 1, "date": "2026-10-18", "status": "failed", "attempts": 1, "last_attempt_at": TS,
        "last_brief_at": None, "sent_by": "manual", "chunks_sent": 1, "digest": digest, "polish_date": None,
        "since": TS - 3600}))
    (config.state_dir / "brief-chunks.json").write_text(json.dumps({
        "version": 1, "date": "2026-10-18", "mode": "counts", "digest": digest, "chunks": chunks}))
    state, corrupt = load_state(config)
    assert not corrupt and state["version"] == 2
    assert state["targets"] == {"discord": {"chunks_sent": 1, "digest": digest, "done": False}}
    hook = Hook()
    assert run_brief(config, True, False, now=NOW + timedelta(minutes=6), webhook=hook, note=lambda s: None,
                     sleep=lambda s: None) == 0
    assert hook.posts == ["part two"]
    saved = json.loads((config.state_dir / "brief.json").read_text())
    assert saved["version"] == 2 and saved["status"] == "sent" and "chunks_sent" not in saved


def test_send_without_any_channel(tmp_path):
    config = cfg(tmp_path)
    notes = []
    assert run_brief(config, True, False, now=NOW, targets=[], note=notes.append) == 2
    assert run_brief(config, True, False, now=NOW, note=notes.append) == 2
    assert notes[0] == brief_mod.NO_CHANNEL and "PA_DISCORD_WEBHOOK" in notes[0]


def test_scheduler_picks_telegram_when_paired_and_discord_counts_otherwise(tmp_path):
    config = cfg(tmp_path, discord_webhook=HOOK, telegram_token=TOKEN, brief_time=(7, 30))
    fill_vault(config.vault_path)
    hook, tg = Hook(), FakeTelegram()
    notifier = Notifier(config, {"discord": DiscordTarget(hook), "telegram": tg}, run_thread=False)
    assert BriefScheduler(config, clock=lambda: TS, notifier=notifier).tick() == "sent"
    assert hook.posts == [] and len(tg.posts) >= 1 and "line 0 https[:]//e.vil/0" in tg.posts[0]

    other = cfg(tmp_path / "b", discord_webhook=HOOK, telegram_token=TOKEN, brief_time=(7, 30))
    fill_vault(other.vault_path)
    hook2, tg2 = Hook(), FakeTelegram(ready=False)
    notifier2 = Notifier(other, {"discord": DiscordTarget(hook2), "telegram": tg2}, run_thread=False)
    assert BriefScheduler(other, clock=lambda: TS, notifier=notifier2).tick() == "sent"
    assert tg2.posts == [] and len(hook2.posts) == 1 and "line 0" not in hook2.posts[0]  # counts only


def test_scheduler_polishes_for_a_full_telegram_brief(tmp_path, monkeypatch):
    config = cfg(tmp_path, telegram_token=TOKEN, brief_time=(7, 30), brief_polish=True, router_small_model="haiku")
    fill_vault(config.vault_path)
    calls = []

    class Stub:
        config = BridgeConfig(workdir=Path("/nonexistent"), config_dir=Path("/nonexistent"), tools=())

        def send(self, text, on_event=None):
            calls.append(text)
            from personal_agent.bridge import TurnResult
            return TurnResult("Ship it.", "sid", False, "success", [], 0.0, None)

        def close(self, timeout_s=10):
            pass

    monkeypatch.setattr(brief_mod, "_polish_session", lambda c: Stub())
    monkeypatch.setattr(brief_mod.shutil, "rmtree", lambda *a, **kw: None)
    tg = FakeTelegram()
    notifier = Notifier(config, {"telegram": tg}, run_thread=False)
    assert BriefScheduler(config, clock=lambda: TS, notifier=notifier).tick() == "sent"
    assert len(calls) == 1 and tg.posts[0].startswith("Brief 2026-10-18 (Sunday)\nShip it.")


def test_scheduler_gives_up_with_one_info_notice(tmp_path):
    config = cfg(tmp_path, discord_webhook=HOOK, brief_time=(7, 30), notify_routes=routes(info="discord"))
    fill_vault(config.vault_path)
    hook = Hook(fail=lambda text, n: "Brief" in text)
    notifier = Notifier(config, {"discord": DiscordTarget(hook)}, run_thread=False)
    clock = Clock(TS)
    scheduler = BriefScheduler(config, clock=clock, notifier=notifier)
    results = []
    for offset in (0, 301, 602, 903, 1300):
        clock.now = TS + offset
        results.append(scheduler.tick())
    assert results == ["failed", "failed", "failed", "gave_up", "gave_up"]
    infos = [p for p in hook.posts if "could not be sent today" in p]
    assert len(infos) == 1


def test_scheduler_without_a_channel_logs_the_reason(tmp_path, caplog):
    config = cfg(tmp_path, telegram_token=TOKEN, brief_time=(7, 30), notify_routes=routes(brief="telegram"))
    notifier = Notifier(config, {"telegram": FakeTelegram(ready=False)}, run_thread=False)
    with caplog.at_level(logging.WARNING):
        assert BriefScheduler(config, clock=lambda: TS, notifier=notifier).tick() == "failed"
    assert "morning brief: no brief channel ready (telegram not paired)" in caplog.text
