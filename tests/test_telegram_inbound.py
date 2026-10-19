"""Inbound Telegram chat in serve: the same Router as the chat, owner only, one turn at a time."""

import json
import logging
import threading
import time
from types import SimpleNamespace

import pytest

from fake_telegram import BOT_ID, OWNER, TOKEN, FakeBotAPI
from personal_agent import telegram
from personal_agent.remote import ApprovalInbox
from personal_agent.router import LOCAL_ONLY, Reply, SafetyLatch
from personal_agent.telegram import OffsetStore, Owner, TelegramClient
from personal_agent.telegram_bot import Outbox, TelegramFrontend, TelegramPoller
from tg_helpers import PIN, block_network, make_config, start_bot, wait_text, wait_until


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    block_network(monkeypatch)


@pytest.fixture
def bot(tmp_path, monkeypatch):
    ns = start_bot(tmp_path, monkeypatch)
    ns.wait_polling()
    yield ns
    ns.close()


# --- the poller -------------------------------------------------------------------


def test_offset_is_saved_before_dispatch_and_nothing_replays(tmp_path, monkeypatch):
    api = FakeBotAPI(max_poll_s=0.1)
    try:
        client = TelegramClient(TOKEN, api.base)
        tmp_path.joinpath("state").mkdir()
        offsets = OffsetStore(tmp_path / "state", BOT_ID)
        handled = []

        def handle(update):
            handled.append(update["update_id"])
            if len(handled) == 1:
                raise RuntimeError("crash while handling")

        stop = threading.Event()
        api.push_message(text="one")
        api.push_message(text="two")
        poller = TelegramPoller(client, offsets, handle, stop=stop, poll_timeout_s=0)
        assert poller.poll_once() == 2  # the crash in the first update does not stop the second
        assert offsets.load() == 3
        restarted = TelegramPoller(client, OffsetStore(tmp_path / "state", BOT_ID), handle, stop=stop,
                                   poll_timeout_s=0)
        assert restarted.poll_once() == 0 and handled == [1, 2]
        # A failed offset save means: do not dispatch (no replay after a crash).
        monkeypatch.setattr(offsets, "save", lambda *a, **k: False)
        api.push_message(text="three")
        with pytest.raises(telegram.TelegramError):
            poller.poll_once()
        assert handled == [1, 2]
    finally:
        api.close()


def test_oversized_updates_from_a_stranger_cannot_deafen_the_bot(tmp_path, monkeypatch):
    api = FakeBotAPI(max_poll_s=0.1)
    try:
        monkeypatch.setattr(telegram, "MAX_UPDATES_BYTES", 300_000)
        client = TelegramClient(TOKEN, api.base)
        (tmp_path / "state").mkdir()
        offsets = OffsetStore(tmp_path / "state", BOT_ID)
        seen = []
        poller = TelegramPoller(client, offsets, lambda u: seen.append(u), stop=threading.Event(), poll_timeout_s=0)
        for _ in range(30):
            api.push_message(chat_id=666, text="😀" * 2048)  # 4096 UTF-16 units each
        api.push_message(text="owner here")
        for _ in range(80):
            poller.poll_once()
            if any(u.get("message", {}).get("text") == "owner here" for u in seen):
                break
        assert any(u["message"]["text"] == "owner here" for u in seen)
        # One update alone above the cap: skipped by its id from the truncated prefix.
        monkeypatch.setattr(telegram, "MAX_UPDATES_BYTES", 500)
        api.push_message(chat_id=666, text="x" * 3000)
        api.push_message(text="after")
        monkeypatch.setattr(telegram, "MAX_UPDATES_BYTES", 500)
        for _ in range(6):
            poller.poll_once()
        monkeypatch.setattr(telegram, "MAX_UPDATES_BYTES", 300_000)
        poller.poll_once()
        assert seen[-1]["message"]["text"] == "after"
    finally:
        api.close()


# --- owner text through the Router ------------------------------------------------


def test_owner_text_gets_a_model_reply(bot):
    bot.api.push_message(text="hello")
    assert wait_text(bot.api, "echo: hello") == "echo: hello"
    [start] = bot.starts()
    assert start["cwd"] == str(bot.vault)


def test_tier1_commands_work_without_a_model(bot):
    api = bot.api
    for text, needle in (("help", "Commands answered without a model"), ("status", LOCAL_ONLY),
                         ("sessions", LOCAL_ONLY), ("note buy milk", "noted in raw/inbox-"),
                         ("tasks", "no tasks"), ("task do the research", "it runs when a chat is open"),
                         ("/config", "unknown command"), ("quota reset", "Only in the terminal."),
                         ("/quota reset", "Only in the terminal."), ("/exit", "Not available over Telegram."),
                         ("/start", "Approvals: buttons")):
        api.push_message(text=text)
        wait_text(api, needle)
    assert bot.starts() == []  # no Claude Code process for any of them
    [inbox] = list((bot.vault / "raw").iterdir())
    assert "[via telegram] buy milk" in inbox.read_text()
    board = json.loads((bot.config.state_dir / "tasks.json").read_text())
    assert board["tasks"][0]["origin"] == "telegram"


def test_new_resets_the_conversation(bot):
    bot.api.push_message(text="hello")
    wait_text(bot.api, "echo: hello")
    runtime = bot.service.frontend()._runtime
    assert runtime.main_session.session_id
    bot.api.push_message(text="/new")
    wait_text(bot.api, "Started a new conversation.")
    assert runtime.main_session.session_id is None


def test_strangers_get_nothing_and_are_logged_without_content(bot, caplog):
    caplog.set_level(logging.INFO, logger="personal_agent.telegram_bot")
    api = bot.api
    api.push_message(chat_id=777, text="hello from a stranger")
    api.push_message(chat_id=-100123, user_id=OWNER, chat_type="group", text="group message")
    api.push_message(chat_id=OWNER, user_id=999, text="not the owner")
    api.push_callback(777, 777, 5, "a" * 22)
    api.push_message(text="tasks")
    wait_text(api, "no tasks")
    time.sleep(0.2)
    assert all(b.get("chat_id") == OWNER for b in api.calls_of("sendMessage"))
    assert api.calls_of("answerCallbackQuery") == []
    lines = [r.getMessage() for r in caplog.records if "ignored" in r.getMessage()]
    assert len(lines) == 1 and "stranger" not in lines[0] and "777" not in lines[0]


def test_busy_queue_and_limits(bot):
    api = bot.api
    api.push_message(text="SLEEP 2")
    assert wait_until(lambda: bot.service.frontend()._busy.is_set())
    for i in range(4):
        api.push_message(text=f"msg {i}")
    for n in (1, 2, 3):
        wait_text(api, f"Queued ({n} ahead).")
    wait_text(api, "Busy: a turn is running and 3 messages are waiting.")
    wait_text(api, "slept 2")


def test_inbound_off_keeps_the_router_away(tmp_path, monkeypatch):
    ns = start_bot(tmp_path, monkeypatch, telegram_inbound=False)
    try:
        ns.wait_polling()
        ns.api.push_message(text="hello")
        wait_text(ns.api, "Chat over Telegram is off (PA_TELEGRAM_INBOUND).")
        assert ns.service.frontend()._runtime is None and ns.starts() == []
    finally:
        ns.close()


def test_a_safety_stop_is_shared_with_the_console(bot, tmp_path, monkeypatch, capsys):
    api = bot.api
    api.push_message(text="HOOK")
    wait_text(api, "Stopped:")
    wait_text(api, "stopped by a safety stop")  # one info notice (Telegram is ready, info goes there)
    api.push_message(text="hello")
    wait_text(api, "Not sent: the assistant was stopped earlier")
    assert (bot.config.state_dir / "safety-stop.json").exists()
    # The console chat sees the same stop at start (no terminal to confirm: it stays).
    from personal_agent.cli import main

    for name, value in (("PA_STATE_DIR", str(bot.config.state_dir)), ("PA_VAULT_DIR", str(bot.vault)),
                        ("PA_CLAUDE_BIN", bot.config.claude_bin), ("PA_ROUTER_SMALL_MODEL", "off"),
                        ("PA_CLAUDE_CONFIG_DIR", str(bot.config.claude_config_dir)), ("PA_TASKS", "off")):
        monkeypatch.setenv(name, value)
    inputs = iter(["hello", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    assert main(["chat"]) == 0
    out = capsys.readouterr().out
    assert "[safety] the assistant was stopped earlier" in out and "Not sent: the assistant was stopped" in out
    assert main(["safety", "clear"]) == 0
    assert not (bot.config.state_dir / "safety-stop.json").exists()


def test_a_console_stop_halts_telegram_turns(bot):
    SafetyLatch(bot.config.state_dir).trip("a hook ran inside the assistant session")
    bot.api.push_message(text="hello")
    wait_text(bot.api, "stopped in another session")
    assert bot.starts() == []


def test_lock_from_the_phone_unpairs(bot):
    bot.api.push_message(text="/lock")
    wait_text(bot.api, "Telegram is locked")
    assert wait_until(lambda: not bot.service.polling)
    assert not (bot.config.state_dir / "telegram-owner.json").exists()


# --- the frontend with a stand-in runtime ---------------------------------------------


class FakeRouter:
    def __init__(self):
        self.halted = None

    def is_tier1(self, text):
        return text == "tasks"

    def help_text(self):
        return "help"


class FakeRuntime:
    def __init__(self, reply_text="ok"):
        self.turns = []
        self.router = FakeRouter()
        self.reply_text = reply_text
        self.parts = SimpleNamespace(config_dir=None)
        self.main_session = SimpleNamespace(abort=lambda: None)

    def turn(self, text, on_event=None):
        self.turns.append(text)
        return Reply(self.reply_text, 3)

    def reset_conversation(self):
        pass

    def idle_close(self):
        pass

    def close(self):
        pass


@pytest.fixture
def front(tmp_path, monkeypatch):
    monkeypatch.setattr(telegram, "SEND_GAP_S", 0.0)
    api = FakeBotAPI(max_poll_s=0.1)
    config = make_config(tmp_path, telegram_max_turns_per_hour=2)
    config.state_dir.mkdir(parents=True)
    client = TelegramClient(TOKEN, api.base)
    clock = [1_000_000.0]
    inbox = ApprovalInbox(None, None, pin=PIN, clock=lambda: clock[0], telegram=SimpleNamespace(
        send=lambda entry: (OWNER, 50 + entry.sequence)))
    runtime = FakeRuntime("x" * 30000)
    outbox = Outbox(client)
    frontend = TelegramFrontend(config, client, Owner(BOT_ID, OWNER, OWNER, 0.0), outbox, lambda: runtime, inbox,
                                None, OffsetStore(config.state_dir, BOT_ID), clock=lambda: clock[0])
    frontend.start()
    ids = iter(range(1, 10_000))

    def message(text=None, **extra):
        payload = {"message_id": next(ids), "date": int(clock[0]), "chat": {"id": OWNER, "type": "private"},
                   "from": {"id": OWNER, "is_bot": False}}
        if text is not None:
            payload["text"] = text
        payload.update(extra)
        frontend.handle_update({"update_id": 1, "message": payload})

    yield SimpleNamespace(api=api, frontend=frontend, runtime=runtime, inbox=inbox, clock=clock, message=message,
                          config=config)
    frontend.close()
    outbox.close()
    api.close()


def test_long_replies_are_chunked_with_a_cut_note(front):
    front.message("hello")
    api = front.api
    assert wait_until(lambda: len(api.calls_of("sendMessage")) == 5)
    texts = api.sent_texts()
    assert all(telegram.utf16_len(t) <= 4000 for t in texts)
    assert texts[-1].endswith("(reply cut: too long for Telegram)")


def test_stale_non_text_forwarded_rate_and_turn_limits(front):
    api = front.api
    front.message("old", date=int(front.clock[0]) - 600)
    wait_text(api, "sent while the agent was offline")
    front.message(None, photo=[{"file_id": "x"}])
    wait_text(api, "Only text messages are supported.")
    front.message("please do this", forward_origin={"type": "user"})
    wait_text(api, "Forwarded messages are not processed")
    front.message("via a bot", via_bot={"id": 1, "is_bot": True})
    front.message("one")
    front.message("two")
    front.message("three")  # PA_TELEGRAM_MAX_TURNS_PER_HOUR=2 in this fixture
    wait_text(api, "hourly limit of model turns")
    front.message("tasks")  # tier 1 is free
    assert wait_until(lambda: "tasks" in front.runtime.turns)
    assert "please do this" not in front.runtime.turns and "via a bot" not in front.runtime.turns
    assert "three" not in front.runtime.turns
    for i in range(20):
        front.message(f"spam {i}")
    wait_text(api, "Slow down")


def test_replayed_owner_messages_are_dropped(front):
    payload = {"message_id": 500, "date": int(front.clock[0]), "chat": {"id": OWNER, "type": "private"},
               "from": {"id": OWNER, "is_bot": False}, "text": "once"}
    front.frontend.handle_update({"update_id": 1, "message": payload})
    front.frontend.handle_update({"update_id": 2, "message": dict(payload)})
    front.frontend.handle_update({"update_id": 3, "message": {**payload, "message_id": 499, "text": "older"}})
    assert wait_until(lambda: front.runtime.turns == ["once"])
    time.sleep(0.2)
    assert front.runtime.turns == ["once"]
    assert OffsetStore(front.config.state_dir, BOT_ID).last_owner_message_id() == 500


def test_the_pin_never_reaches_the_assistant(front, caplog):
    caplog.set_level(logging.DEBUG)
    inbox = front.inbox
    entry = inbox.register("Write", 3, "summary", 300, via="telegram", origin="telegram")
    assert inbox.press(entry.approve_token, OWNER, entry.message_id) == "need_pin"
    front.message("an ordinary message")
    wait_text(front.api, "Waiting for your PIN; message not sent.")
    assert inbox.pin_attempts_left(entry) == 3  # ordinary text is not eaten as a guess
    front.clock[0] += 130  # the PIN wait (120 s) is over
    front.message(PIN)
    wait_text(front.api, "That looks like your PIN; it was not sent to the assistant.")
    front.message(f"  {front.config.call_pin} ")
    assert wait_until(lambda: len(front.api.calls_of("deleteMessage")) == 2)
    assert front.runtime.turns == []
    assert PIN not in caplog.text and PIN not in json.dumps(front.api.calls_of("sendMessage"))
