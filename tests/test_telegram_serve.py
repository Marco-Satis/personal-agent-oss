"""serve with Telegram: supervisor, generations, shutdown, alerts, hijack and rejection."""

import ast
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import personal_agent
from fake_telegram import BOT_ID, OWNER, FakeBotAPI
from personal_agent.escalation import Event
from personal_agent.notify import DiscordTarget
from personal_agent.quota import QuotaGuard
from personal_agent.server import make_server
from personal_agent.telegram import PollLock
from tg_helpers import API_TOKEN, block_network, pair, start_bot, wait_text, wait_until


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    block_network(monkeypatch)


class Hook:
    def __init__(self):
        self.posts = []

    def post(self, text):
        self.posts.append(text)
        return True


def test_the_supervisor_follows_the_pairing(tmp_path, monkeypatch):
    ns = start_bot(tmp_path, monkeypatch, paired=False)
    try:
        time.sleep(0.4)
        assert not ns.service.polling
        assert ns.api.calls_of("getUpdates") == [] and ns.api.calls == []  # unpaired: no Telegram traffic
        assert not PollLock(ns.config.state_dir).held_elsewhere()  # free for `telegram pair`
        pair(ns.config)
        ns.wait_polling()
        assert PollLock(ns.config.state_dir).held_elsewhere()
        assert ns.api.calls_of("getWebhookInfo")  # checked at the start of each generation
        (ns.config.state_dir / "telegram-owner.json").unlink()
        assert wait_until(lambda: not ns.service.polling)
        assert wait_until(lambda: not PollLock(ns.config.state_dir).held_elsewhere())
    finally:
        ns.close()


def test_owner_change_denies_pending_and_starts_fresh(tmp_path, monkeypatch):
    ns = start_bot(tmp_path, monkeypatch)
    try:
        ns.wait_polling()
        ns.api.push_message(text="hello")
        wait_text(ns.api, "echo: hello")
        old = ns.service.frontend()
        ns.api.push_message(text="WRITE a.md")
        ns.api.wait_for("sendMessage", lambda b: "reply_markup" in b)
        pair(ns.config, chat_id=5555)
        assert wait_until(lambda: ns.service.frontend() is not None and ns.service.frontend() is not old)
        # The old owner's pending approval is denied at once and its prompt closed.
        assert ns.api.wait_for("editMessageText")["text"].endswith("→ denied")
        assert ns.inbox.pending_count() == 0
        assert ns.service.frontend()._runtime is None  # never the old conversation
        assert not (ns.vault / "a.md").exists()
    finally:
        ns.close()


def test_close_joins_everything_while_a_turn_and_an_approval_run(tmp_path, monkeypatch):
    ns = start_bot(tmp_path, monkeypatch)
    closed = False
    try:
        ns.wait_polling()
        ns.api.push_message(text="WRITE a.md")
        ns.api.wait_for("sendMessage", lambda b: "reply_markup" in b)
        assert ns.inbox.pending_count() == 1
        pids = [s["pid"] for s in ns.starts()]
        assert pids
        started = time.monotonic()
        ns.close()
        closed = True
        assert time.monotonic() - started < 8
        names = {t.name for t in threading.enumerate() if t.is_alive()}
        assert not names & {"telegram-supervisor", "telegram-turns", "telegram-outbox"}
        assert ns.inbox.pending_count() == 0
        for pid in pids:
            assert wait_until(lambda: not _alive(pid), timeout=5)
    finally:
        if not closed:
            ns.close()


def test_close_stops_a_long_turn(tmp_path, monkeypatch):
    ns = start_bot(tmp_path, monkeypatch)
    try:
        ns.wait_polling()
        ns.api.push_message(text="SLEEP 30")
        assert wait_until(lambda: ns.starts(), timeout=5)
        time.sleep(0.3)
        pids = [s["pid"] for s in ns.starts()]
        started = time.monotonic()
    finally:
        ns.close()
    assert time.monotonic() - started < 8
    for pid in pids:
        assert wait_until(lambda: not _alive(pid), timeout=5)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie of an already finished child counts as gone
        status = Path(f"/proc/{pid}/status").read_text()
        return "State:\tZ" not in status
    except OSError:
        return True


def test_escalation_alerts_go_to_telegram_without_delaying_the_call(tmp_path, monkeypatch):
    ns = start_bot(tmp_path, monkeypatch)
    try:
        ns.api.fail("sendMessage", 200, delay=3.0, body=b'{"ok":true,"result":{"message_id":7}}')
        started = time.monotonic()
        assert ns.agent.escalate(Event("disk", "critical", "Disk full, see https://e.vil"))["called"] is True
        ns.agent.escalate(Event("disk", "critical", "Disk full, see https://e.vil"))
        assert time.monotonic() - started < 1.0
        ns.notifier.close(10.0)
        alerts = [b for b in ns.api.calls_of("sendMessage") if "Alert" in b["text"]]
        assert len(alerts) == 1
        assert alerts[0]["text"] == "Alert (critical) disk: Disk full, see https[:]//e.vil — calling you now"
    finally:
        ns.close()


def test_a_webhook_set_by_someone_else_is_a_hijack(tmp_path, monkeypatch):
    api = FakeBotAPI(max_poll_s=0.2)
    api.webhook_url = "https://attacker.example/hook"
    ns = start_bot(tmp_path, monkeypatch, api=api, notify_routes=(("info", ("telegram", "discord")),),
                   discord_webhook="https://discord.com/api/webhooks/1/x")
    hook = Hook()
    ns.notifier.targets["discord"] = DiscordTarget(hook)
    try:
        assert wait_until(lambda: ns.service.hijacked)
        assert not ns.service.polling and ns.api.calls_of("getUpdates") == []
        assert ns.api.calls_of("deleteWebhook") == []  # never hidden
        assert wait_until(lambda: any("token may be leaked" in p for p in hook.posts))
        assert not any("leaked" in b["text"] for b in ns.api.calls_of("sendMessage"))
    finally:
        ns.close()


def test_a_conflict_while_we_hold_the_lock_is_a_hijack(tmp_path, monkeypatch):
    from personal_agent import telegram_bot

    monkeypatch.setattr(telegram_bot, "CONFLICT_WAIT_S", 0.05)
    api = FakeBotAPI(max_poll_s=0.2)
    ns = start_bot(tmp_path, monkeypatch, api=api)
    try:
        ns.wait_polling()
        ns.api.push_message(text="WRITE a.md")
        ns.api.wait_for("sendMessage", lambda b: "reply_markup" in b)
        api.fail("getUpdates", 409, times=1)  # a single 409: someone else polls with our token
        assert wait_until(lambda: ns.service.hijacked, timeout=10)
        assert wait_until(lambda: not ns.service.polling)
        assert ns.inbox.pending_count() == 0  # pending Telegram approvals are denied
    finally:
        ns.close()


class ConflictClient:
    """getUpdates answers from a script: "409" raises a conflict, anything else is an empty batch."""

    def __init__(self, script, stop):
        self.script = list(script)
        self.stop = stop

    def get_updates(self, offset, timeout_s, limit):
        from personal_agent.telegram import TelegramError

        if not self.script:
            self.stop.set()
            return []
        step = self.script.pop(0)
        if step == "409":
            raise TelegramError("conflict", 409)
        return []


def test_conflicts_interleaved_with_successful_polls_still_count(tmp_path, monkeypatch):
    from personal_agent import telegram_bot
    from personal_agent.telegram import OffsetStore

    monkeypatch.setattr(telegram_bot, "CONFLICT_WAIT_S", 0.0)
    stop = threading.Event()
    seen = []
    poller = telegram_bot.TelegramPoller(ConflictClient(["409", "ok", "409", "ok", "409", "ok"], stop),
                                         OffsetStore(tmp_path, 1), lambda u: None, stop=stop, poll_timeout_s=0,
                                         on_conflict=seen.append)
    poller.run()
    assert seen == [1, 2, 3]  # a successful poll in between never resets the count
    assert telegram_bot.CONFLICTS_FOR_HIJACK == 1  # and the first one already is a hijack


def test_a_stopped_generation_ignores_its_own_abandoned_poll(tmp_path):
    from personal_agent import telegram_bot
    from personal_agent.telegram import OffsetStore

    stop = threading.Event()
    seen = []

    class Abandoned(ConflictClient):
        def get_updates(self, offset, timeout_s, limit):
            stop.set()  # the generation stopped while its long poll was in flight
            return super().get_updates(offset, timeout_s, limit)

    poller = telegram_bot.TelegramPoller(Abandoned(["409"], stop), OffsetStore(tmp_path, 1), lambda u: None,
                                         stop=stop, poll_timeout_s=0, on_conflict=seen.append)
    poller.run()
    assert seen == []  # Telegram ended the older request: that is not a foreign poller


def test_repairing_to_another_chat_while_serve_runs(tmp_path, monkeypatch):
    from personal_agent.telegram import OffsetStore

    ns = start_bot(tmp_path, monkeypatch)
    try:
        ns.wait_polling()
        ns.api.push_message(text="tasks", message_id=3000)
        wait_text(ns.api, "task")
        old = ns.service.frontend()
        # `telegram unpair`, then `telegram pair` with another account, from another process.
        (ns.config.state_dir / "telegram-owner.json").unlink()
        assert wait_until(lambda: not ns.service.polling)
        OffsetStore(ns.config.state_dir, BOT_ID).save(offset=ns.service.offsets_store.load() or 0,
                                                       last_owner_message_id=0, chat_id=5555)
        pair(ns.config, chat_id=5555)
        assert wait_until(lambda: ns.service.frontend() is not None and ns.service.frontend() is not old)
        ns.api.push_message(chat_id=5555, user_id=5555, text="hello from the new owner")
        wait_text(ns.api, "echo: hello from the new owner", chat_id=5555)
    finally:
        ns.close()


def test_the_replay_guard_belongs_to_one_chat(tmp_path):
    from personal_agent.telegram import OffsetStore

    serve_store = OffsetStore(tmp_path, BOT_ID)
    assert serve_store.save(offset=50, last_owner_message_id=1500, chat_id=OWNER)
    assert serve_store.last_owner_message_id(OWNER) == 1500
    assert serve_store.last_owner_message_id(5555) is None  # message ids count per chat
    OffsetStore(tmp_path, BOT_ID).save(offset=80, last_owner_message_id=0, chat_id=5555)  # `telegram pair`
    assert serve_store.load() == 80 and serve_store.last_owner_message_id(5555) == 0
    assert serve_store.last_owner_message_id(OWNER) is None


def test_a_rejected_token_stops_polling_and_tells_other_channels(tmp_path, monkeypatch):
    api = FakeBotAPI(max_poll_s=0.2)
    ns = start_bot(tmp_path, monkeypatch, api=api, notify_routes=(("info", ("discord",)),),
                   discord_webhook="https://discord.com/api/webhooks/1/x")
    hook = Hook()
    ns.notifier.targets["discord"] = DiscordTarget(hook)
    try:
        ns.wait_polling()
        api.fail("getUpdates", 401, times=5)
        api.fail("getMe", 401, times=5)
        assert wait_until(lambda: ns.service.client.rejected and not ns.service.polling, timeout=10)
        assert wait_until(lambda: any("token was rejected" in p for p in hook.posts))
        assert ns.service.target.ready() == "telegram token rejected"
    finally:
        ns.close()


def test_task_notices_reach_telegram_from_the_chat_process(tmp_path, monkeypatch):
    from personal_agent.coordinator import TaskEvent, TaskNotifier
    from personal_agent.notify import Notifier, NotifierPost

    ns = start_bot(tmp_path, monkeypatch)
    try:
        messenger = Notifier.from_config(ns.config, run_thread=False)  # what the chat builds
        assert messenger.telegram_ready()
        notifier = TaskNotifier(NotifierPost(messenger, "task_done"), None, run_thread=False)
        notifier.record(TaskEvent("abc123", "done", "outputs/tasks/abc123.md", None, title="Secret"))
        assert notifier.flush()
        text = wait_text(ns.api, "Tasks: 1 done")
        assert "abc123" not in text and "Secret" not in text
    finally:
        ns.close()


def test_quota_guards_see_each_other(tmp_path):
    path = tmp_path / "quota.json"
    chat, telegram_guard = QuotaGuard(0.9, path), QuotaGuard(0.9, path)
    assert telegram_guard.check().ok
    chat.observe({"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.95}}})
    time.sleep(0.01)
    check = telegram_guard.check()
    assert not check.ok and "five-hour window" in check.reason
    # `quota reset` in the terminal (Telegram refuses it) also clears serve's guard.
    chat.reset()
    assert chat.check().ok
    assert telegram_guard.check().ok


def test_the_telegram_broker_never_delegates(tmp_path):
    from personal_agent.approval import ApprovalBroker

    broker = ApprovalBroker(tmp_path, main_label="telegram")
    assert broker.main.label == "telegram" and not broker.delegation_allowed
    with pytest.raises(RuntimeError):
        broker.enable_delegation()
    broker.on_delegate = lambda text, domain: "abc123"
    broker.main.delegation = True  # even if something set it, the broker refuses
    verdict = broker._delegate(broker.main, {"text": "x"}, "0" * 64)
    assert not verdict.allow and verdict.message == "delegation is off"
    with pytest.raises(ValueError):
        ApprovalBroker(tmp_path, main_label="root")


def test_serve_never_builds_a_coordinator():
    package = Path(personal_agent.__file__).resolve().parent
    for name in ("server.py", "telegram.py", "telegram_bot.py"):
        tree = ast.parse((package / name).read_text(encoding="utf-8"))
        called = {node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
                  for node in ast.walk(tree) if isinstance(node, ast.Call)}
        assert "Coordinator" not in called and "session_overview" not in called


def test_sigterm_shuts_serve_down_cleanly(tmp_path):
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "PA_STATE_DIR": str(tmp_path / "state"),
           "PA_API_TOKEN": API_TOKEN, "PA_CALL_TO": "+447700900123", "PA_CALL_PIN": "482913", "PA_PORT": "0",
           "PYTHONPATH": str(Path(personal_agent.__file__).resolve().parent.parent)}
    proc = subprocess.Popen([sys.executable, "-m", "personal_agent", "serve"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        line = ""
        end = time.monotonic() + 10
        while "listening" not in line and time.monotonic() < end:
            line = proc.stderr.readline()
        assert "listening" in line
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_internal_api_answers_not_available_when_unpaired(tmp_path, monkeypatch):
    ns = start_bot(tmp_path, monkeypatch, paired=False)
    server = make_server(ns.agent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/internal/approvals",
            data=json.dumps({"tool_name": "Write", "level": 2, "summary": "s", "timeout_s": 60}).encode(),
            method="POST", headers={"Authorization": f"Bearer {API_TOKEN}", "Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as info:
            urllib.request.urlopen(req, timeout=10)
        assert info.value.code == 429
        info.value.close()
    finally:
        server.shutdown()
        server.server_close()
        ns.close()
    assert OWNER


def test_brief_send_goes_to_the_paired_bot_as_plain_text(tmp_path, monkeypatch):
    from personal_agent.brief import run_brief
    from personal_agent.vault import Vault

    ns = start_bot(tmp_path, monkeypatch)
    try:
        vault = Vault(ns.vault)
        vault.init()
        (ns.vault / "wiki" / "todo.md").write_text("- [ ] renew the **passport** https://e.vil/x\n")
        notes = []
        assert run_brief(ns.config, True, False, note=notes.append, sleep=lambda s: None) == 0  # targets: Notifier
        text = wait_text(ns.api, "Brief ")
        assert "renew the **passport** https[:]//e.vil/x" in text and "https://" not in text
        body = ns.api.calls_of("sendMessage")[-1]
        assert "parse_mode" not in body and body["link_preview_options"] == {"is_disabled": True}
    finally:
        ns.close()
