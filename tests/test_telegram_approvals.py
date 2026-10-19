"""Approvals over Telegram: buttons, single use, owner only, expiry, level-3 PIN, console remote."""

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from fake_telegram import OWNER
from personal_agent import remote
from personal_agent.approval import ApprovalRequest, Level
from personal_agent.config import ConfigError
from personal_agent.remote import ApprovalInbox, RemoteChannel
from personal_agent.server import make_server
from tg_helpers import API_TOKEN, PIN, block_network, make_config, start_bot, wait_text, wait_until

PUBLIC = "https://agent.example.org"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    block_network(monkeypatch)


@pytest.fixture
def bot(tmp_path, monkeypatch):
    ns = start_bot(tmp_path, monkeypatch)
    ns.wait_polling()
    yield ns
    ns.close()


def prompt_of(api, timeout=10.0):
    return api.wait_for("sendMessage", lambda b: "reply_markup" in b, timeout=timeout)


def buttons(prompt):
    return [button for row in prompt["reply_markup"]["inline_keyboard"] for button in row]


def press(api, prompt, index, message_id=None, user=OWNER, chat=OWNER):
    data = buttons(prompt)[index]["callback_data"]
    mid = message_id if message_id is not None else prompt["_mid"]
    return api.push_callback(user, chat, mid, data)


def with_mid(bot, prompt):
    """The message id the fake returned for the prompt."""
    prompt["_mid"] = prompt["_message_id"]
    return prompt


def test_approve_from_the_owner_writes_and_closes_the_prompt(bot):
    api = bot.api
    api.push_message(text="WRITE a.md")
    prompt = with_mid(bot, prompt_of(api))
    text = prompt["text"]
    assert text.startswith("Approval #1 needed · telegram") and "Level 2 (write): Write" in text
    assert "a.md" not in text and "content" not in text  # never the tool input
    data = [b["callback_data"] for b in buttons(prompt)]
    assert len(data) == 3 and len(set(data)) == 3
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{22}", d) and len(d.encode()) <= 64 for d in data)
    assert [b["text"] for b in buttons(prompt)] == ["Approve", "Deny", "Deny and stop turn"]
    press(api, prompt, 0)
    wait_text(api, "wrote a.md")
    assert (bot.vault / "a.md").read_text() == "x"
    assert api.wait_for("answerCallbackQuery")["text"] == "Approved"
    edit = api.wait_for("editMessageReplyMarkup")
    assert edit["reply_markup"] == {"inline_keyboard": []} and edit["message_id"] == prompt["_mid"]
    assert api.wait_for("editMessageText")["text"].endswith("→ approved")


def test_deny_single_use_and_foreign_presses(bot):
    api = bot.api
    api.push_message(text="WRITE b.md")
    prompt = with_mid(bot, prompt_of(api))
    press(api, prompt, 0, user=999, chat=999)          # a stranger: ignored, not even answered
    press(api, prompt, 0, user=OWNER, chat=12345)       # the right token from another chat
    press(api, prompt, 0, message_id=prompt["_mid"] + 50)  # the right token on another message
    assert wait_until(lambda: len(api.calls_of("answerCallbackQuery")) == 1)
    assert api.calls_of("answerCallbackQuery")[0]["text"] == "Expired or already decided"
    assert not (bot.vault / "b.md").exists()
    press(api, prompt, 1)
    wait_text(api, "denied")
    assert api.wait_for("answerCallbackQuery", lambda b: b.get("text") == "Denied")
    press(api, prompt, 0)  # the decision is final
    api.wait_for("answerCallbackQuery", lambda b: b.get("text") == "Expired or already decided", count=2)
    assert not (bot.vault / "b.md").exists()
    assert api.wait_for("editMessageText")["text"].endswith("→ denied")


def test_deny_and_stop_ends_the_turn(bot):
    api = bot.api
    api.push_message(text="WRITE c.md")
    prompt = with_mid(bot, prompt_of(api))
    press(api, prompt, 2)
    api.wait_for("answerCallbackQuery", lambda b: "stopped" in b.get("text", ""))
    assert wait_until(lambda: not bot.service.frontend()._busy.is_set(), timeout=10)
    assert not (bot.vault / "c.md").exists()
    # Stopping one turn never disables the session: the next turns still reach Claude Code.
    api.push_message(text="hello again")
    wait_text(api, "echo: hello again")
    api.push_message(text="WRITE e.md")
    second = with_mid(bot, api.wait_for(
        "sendMessage", lambda b: "reply_markup" in b and b.get("_message_id") != prompt["_mid"], timeout=10))
    press(api, second, 0)
    wait_text(api, "wrote e.md")
    assert (bot.vault / "e.md").read_text() == "x"


def test_lock_works_during_a_pin_wait_and_cuts_off_at_once(bot):
    api = bot.api
    bot.service.tick_s = 30  # the supervisor stays out of it: /lock alone must cut the chat off
    time.sleep(0.2)
    api.push_message(text="WRITE_AT CLAUDE.md")
    prompt = with_mid(bot, prompt_of(api))
    press(api, prompt, 0)
    wait_text(api, "your next message is used as the PIN")
    assert bot.inbox.pin_entry() is not None
    api.push_message(text="/lock")
    wait_text(api, "Telegram is locked")
    assert wait_until(lambda: bot.inbox.pending_count() == 0, timeout=3)
    assert not (bot.config.state_dir / "telegram-owner.json").exists()
    front = bot.service.frontend()
    assert front is not None and front._closed.is_set()  # before any supervisor tick
    press(api, prompt, 0)  # a press after /lock decides nothing
    api.push_message(text="hello")
    time.sleep(0.5)
    assert "echo: hello" not in "\n".join(api.sent_texts())
    assert (bot.vault / "CLAUDE.md").read_text() != "x"

def test_level3_needs_the_pin_and_it_never_leaks(bot, caplog):
    caplog.set_level(logging.DEBUG)
    api = bot.api
    api.push_message(text="WRITE_AT CLAUDE.md")
    prompt = with_mid(bot, prompt_of(api))
    assert "Level 3 (danger)" in prompt["text"]
    press(api, prompt, 0)
    api.wait_for("answerCallbackQuery", lambda b: "PIN" in b.get("text", ""))
    wait_text(api, "your next message is used as the PIN")
    api.push_message(text="000000")
    wait_text(api, "Wrong PIN, 2 attempt(s) left.")
    api.push_message(text=PIN)
    wait_text(api, "Approved.")
    wait_text(api, "wrote CLAUDE.md")
    assert len(api.calls_of("deleteMessage")) == 2  # both PIN messages, best effort
    log_text = bot.log.read_text()
    assert PIN not in log_text and PIN not in caplog.text


def test_three_wrong_pins_deny(bot):
    api = bot.api
    api.push_message(text="WRITE_AT CLAUDE.md")
    prompt = with_mid(bot, prompt_of(api))
    press(api, prompt, 0)
    wait_text(api, "your next message is used as the PIN")
    for pin in ("111111", "222222", "333333"):
        api.push_message(text=pin)
        time.sleep(0.05)
    wait_text(api, "Denied: too many wrong PINs.")
    wait_text(api, "denied")
    assert (bot.vault / "CLAUDE.md").read_text() != "x"  # the vault rules file stays as it was


def test_the_pin_is_required_for_serve(tmp_path):
    config = make_config(tmp_path, approval_pin=None)
    with pytest.raises(ConfigError, match="PA_APPROVAL_PIN"):
        config.validate_for_serve()
    make_config(tmp_path, public_approval_url=PUBLIC).validate_for_serve()  # PUBLIC with Telegram alone is fine
    make_config(tmp_path, brief_time=(7, 30)).validate_for_serve()
    with pytest.raises(ConfigError, match="PA_BRIEF_TIME needs PA_DISCORD_WEBHOOK"):
        make_config(tmp_path, brief_time=(7, 30), telegram_token=None).validate_for_serve()


# --- the inbox, with a fake clock ------------------------------------------------------


class Sender:
    def __init__(self):
        self.sent = []

    def send(self, entry):
        self.sent.append(entry)
        return OWNER, 1000 + len(self.sent)


def make_inbox(clock, tmp_path=None, **kw):
    finals = []
    inbox = ApprovalInbox(None, None, pin=PIN, clock=lambda: clock[0], telegram=Sender(),
                          on_final=finals.append, state_dir=tmp_path, **kw)
    return inbox, finals


def test_expiry_closes_the_prompt_and_later_presses_are_invalid(tmp_path):
    clock = [1000.0]
    inbox, finals = make_inbox(clock)
    entry = inbox.register("Write", 2, "s", 60, via="telegram")
    clock[0] += 60
    inbox.sweep()
    assert finals == [entry] and entry.state == "expired"
    assert inbox.press(entry.approve_token, OWNER, entry.message_id) == "invalid"


def test_pin_lockouts_disable_level3_and_survive_a_restart(tmp_path):
    clock = [1000.0]
    inbox, _ = make_inbox(clock, tmp_path)
    for _ in range(remote.MAX_WRONG_PINS_PER_DAY // remote.MAX_PIN_ATTEMPTS):
        entry = inbox.register("Write", 3, "s", 300, via="telegram")
        assert inbox.press(entry.approve_token, OWNER, entry.message_id) == "need_pin"
        outcomes = [inbox.submit_pin(entry.id, "999999") for _ in range(3)]
        assert outcomes == ["bad_pin", "bad_pin", "locked"]
        clock[0] += 1
    assert inbox.register("Write", 3, "s", 300, via="telegram") is None  # level 3 disabled
    assert inbox.register("Write", 2, "s", 300, via="telegram") is not None
    restarted, _ = make_inbox(clock, tmp_path)
    assert restarted.lockout.disabled()  # persisted: a crash loop does not reset it
    restarted.lockout.clear()
    assert not make_inbox(clock, tmp_path)[0].lockout.disabled()


def test_page_lockouts_count_too(tmp_path):
    clock = [1000.0]
    poster = SimpleNamespace(post=lambda text: True)
    inbox = ApprovalInbox(poster, PUBLIC, pin=PIN, clock=lambda: clock[0], state_dir=tmp_path)
    for _ in range(remote.MAX_WRONG_PINS_PER_DAY // remote.MAX_PIN_ATTEMPTS):
        entry = inbox.register("Write", 3, "s", 300)
        for _ in range(3):
            outcome = inbox.resolve(entry.id, entry.csrf, "allow", "confirm", "000000")
        assert outcome == "locked"
    assert inbox.lockout.disabled()


def test_every_wrong_pin_counts_even_without_a_lockout(tmp_path):
    """Two wrong PINs and then Deny (or letting the wait run out), again and again, must add up."""
    clock = [1000.0]
    inbox, _ = make_inbox(clock, tmp_path)
    outcomes = []
    for round_ in range(3):
        entry = inbox.register("Write", 3, "s", 300, via="telegram")
        assert inbox.press(entry.approve_token, OWNER, entry.message_id) == "need_pin"
        outcomes += [inbox.submit_pin(entry.id, "999999") for _ in range(2)]
        if entry.state == "pending":
            assert inbox.press(entry.deny_token, OWNER, entry.message_id) == "denied"
        clock[0] += 60
    # The sixth wrong PIN within 24 h disables level 3 and denies that request at once.
    assert outcomes == ["bad_pin"] * 5 + ["locked"]
    assert inbox.lockout.disabled() and inbox.register("Write", 3, "s", 300, via="telegram") is None
    assert inbox.lockout.status() == (remote.MAX_WRONG_PINS_PER_DAY, True)
    # The page path shares the budget.
    poster = SimpleNamespace(post=lambda text: True)
    page = ApprovalInbox(poster, PUBLIC, pin=PIN, clock=lambda: clock[0], state_dir=tmp_path / "page")
    for _ in range(3):
        entry = page.register("Write", 3, "s", 300)
        page.resolve(entry.id, entry.csrf, "allow", "confirm", "000000")
        page.resolve(entry.id, entry.csrf, "allow", "confirm", "000001")
        page.resolve(entry.id, entry.csrf, "deny")
    assert page.lockout.disabled()


def test_clear_lock_reaches_a_running_serve(tmp_path):
    clock = [1000.0]
    serve_lockout = remote.PinLockout(tmp_path, clock=lambda: clock[0])
    for _ in range(remote.MAX_WRONG_PINS_PER_DAY):
        serve_lockout.record_wrong()
    assert serve_lockout.disabled()
    remote.PinLockout(tmp_path, clock=lambda: clock[0]).clear()  # `telegram status --clear-lock`
    assert not serve_lockout.disabled()
    assert serve_lockout.status() == (0, False)
    serve_lockout.record_wrong()  # the old record never comes back
    assert remote.PinLockout(tmp_path, clock=lambda: clock[0]).status() == (1, False)
    # And the other way round: a record written by another process is seen.
    other = remote.PinLockout(tmp_path, clock=lambda: clock[0])
    for _ in range(remote.MAX_WRONG_PINS_PER_DAY):
        other.record_wrong()
    assert serve_lockout.disabled()


def test_pressing_approve_again_never_extends_the_pin_wait():
    clock = [1000.0]
    inbox, _ = make_inbox(clock)
    entry = inbox.register("Write", 3, "s", 300, via="telegram")
    assert inbox.press(entry.approve_token, OWNER, entry.message_id) == "need_pin"
    until = entry.pin_until
    clock[0] += 100
    assert inbox.press(entry.approve_token, OWNER, entry.message_id) == "need_pin"
    assert entry.pin_until == until
    clock[0] += 21
    assert inbox.pin_entry() is None


def test_pin_busy_and_shared_notice_limit():
    clock = [1000.0]
    inbox, _ = make_inbox(clock)
    first = inbox.register("Write", 3, "s", 300, via="telegram")
    second = inbox.register("Edit", 3, "s", 300, via="telegram")
    assert inbox.press(first.approve_token, OWNER, first.message_id) == "need_pin"
    assert inbox.press(second.approve_token, OWNER, second.message_id) == "pin_busy"
    assert inbox.press(first.approve_token, OWNER, first.message_id) == "need_pin"  # idempotent
    clock[0] += remote.PIN_WAIT_S + 1
    assert inbox.pin_entry() is None  # the wait ended; the entry is still open
    assert inbox.press(second.approve_token, OWNER, second.message_id) == "need_pin"
    inbox.cancel_all()
    # NOTICE_LIMIT counts Telegram turns and console approvals together (fails closed).
    console = [inbox.register("Write", 2, "s", 300, via="telegram", origin="chat") for _ in range(3)]
    assert all(console) and inbox.register("Write", 2, "s", 300, via="telegram") is None
    assert [e.sequence for e in console] == [3, 4, 5]


# --- console approvals through serve ------------------------------------------------------


@pytest.fixture
def served(bot):
    server = make_server(bot.agent)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield SimpleNamespace(bot=bot, base=f"http://127.0.0.1:{server.server_address[1]}")
    server.shutdown()
    server.server_close()


def request(level=Level.WRITE, scope_label="chat", context=None):
    return ApprovalRequest(tool_name="Write", level=level, summary='file_path="x.md"', tool_use_id="t1",
                           scope_label=scope_label, context=context)


def test_console_remote_approval_goes_to_telegram(served):
    api = served.bot.api
    lines = []
    channel = RemoteChannel(served.base, API_TOKEN, threading.Lock(), write=lines.append, poll_s=1)
    result = []
    thread = threading.Thread(target=lambda: result.append(channel.ask(request(context="note context in this turn"),
                                                                       time.monotonic() + 30)))
    thread.start()
    prompt = with_mid(served.bot, prompt_of(api))
    assert prompt["text"].startswith("Approval #1 needed · chat") and "note text or a plugin skill" in prompt["text"]
    assert len(buttons(prompt)) == 2  # no stop button for a turn of another process
    press(api, prompt, 0)
    thread.join(15)
    assert result == [True] and "[approved remotely]" in lines
    assert any("buttons sent to Telegram" in line for line in lines)


def test_console_remote_cancel_closes_the_prompt(served):
    api = served.bot.api
    channel = RemoteChannel(served.base, API_TOKEN, threading.Lock(), write=lambda line: None, poll_s=1)
    assert channel.ask(request(), time.monotonic() + 8) is False  # nobody presses: the deadline passes
    assert api.wait_for("editMessageText")["text"].endswith("→ expired")


def test_internal_api_validates_origin_and_context(served):
    def register(payload):
        req = urllib.request.Request(served.base + "/internal/approvals", data=json.dumps(payload).encode(),
                                     method="POST", headers={"Authorization": f"Bearer {API_TOKEN}",
                                                             "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, {}

    base = {"tool_name": "Write", "level": 2, "summary": "s", "timeout_s": 60}
    assert register({**base, "origin": "telegram"})[0] == 400  # only serve itself may say telegram
    assert register({**base, "origin": "task 3f2a9c (research) · evil"})[0] == 400
    assert register({**base, "context": "yes"})[0] == 400
    status, reply = register(base)  # an old client without the new fields
    assert status == 200 and reply["via"] == "telegram"
    status, _ = register({**base, "origin": "task 3f2a9c (research) · telegram", "context": True})
    assert status == 200


def test_review_link_and_page_decision_close_the_prompt(tmp_path, monkeypatch):
    ns = start_bot(tmp_path, monkeypatch, public_approval_url=PUBLIC)
    try:
        ns.wait_polling()
        ns.api.push_message(text="WRITE d.md")
        prompt = prompt_of(ns.api)
        match = re.search(r"Review: (https://agent\.example\.org/approve/([A-Za-z0-9_-]{32}))", prompt["text"])
        assert match
        entry = ns.inbox.get(match.group(2))
        assert ns.inbox.resolve(entry.id, entry.csrf, "allow") == "ok"
        wait_text(ns.api, "wrote d.md")
        edited = ns.api.wait_for("editMessageText")["text"]
        assert edited.endswith("→ approved") and "Review:" not in edited and "/approve/" not in edited
    finally:
        ns.close()
