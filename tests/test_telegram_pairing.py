"""Pairing the Telegram bot: one-time code, owner binding, status and unpair."""

import json
import os
import re
import socket
import stat
import threading
import time
from pathlib import Path

import pytest

from fake_telegram import BOT_ID, OWNER, TOKEN, FakeBotAPI
from personal_agent import telegram
from personal_agent.cli import main
from personal_agent.config import Config
from personal_agent.telegram import OwnerStore, PairingStore, PollLock, TelegramClient, run_pairing

PIN = "731946"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    real = socket.socket.connect

    def guarded(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and host not in ("127.0.0.1", "::1", "localhost"):
            raise OSError("network disabled in tests")
        return real(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture
def api(monkeypatch):
    fake = FakeBotAPI(max_poll_s=0.2)
    monkeypatch.setattr(telegram, "DEFAULT_API_BASE", fake.base)
    yield fake
    fake.close()


@pytest.fixture
def config(tmp_path, monkeypatch):
    state = tmp_path / "state"
    for name, value in (("PA_STATE_DIR", str(state)), ("PA_TELEGRAM_TOKEN", TOKEN), ("PA_APPROVAL_PIN", PIN)):
        monkeypatch.setenv(name, value)
    for name in ("PA_DISCORD_WEBHOOK", "PA_PUBLIC_APPROVAL_URL", "PA_NOTIFY_APPROVAL", "PA_NOTIFY_BRIEF"):
        monkeypatch.delenv(name, raising=False)
    return Config.from_env()


class Pairing:
    """run_pairing in a thread, with the printed lines collected."""

    def __init__(self, config, **kwargs):
        self.out, self.err = [], []
        self.result = None
        kwargs.setdefault("print_code", True)
        kwargs.setdefault("yes", True)
        kwargs.setdefault("poll_timeout_s", 1)
        kwargs.setdefault("sleep", lambda s: time.sleep(min(s, 0.05)))

        def run():
            self.result = run_pairing(config, out=self.out.append, err=self.err.append, **kwargs)

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def code(self, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for line in self.out:
                match = re.search(r"\?start=([A-Za-z0-9_-]+) ", line)
                if match:
                    return match.group(1)
            time.sleep(0.02)
        raise AssertionError(f"no code printed: {self.out} {self.err}")

    def join(self, timeout=10.0):
        self.thread.join(timeout)
        assert not self.thread.is_alive()
        return self.result


def state_files(config) -> str:
    return "".join(p.read_text(errors="replace") for p in config.state_dir.iterdir() if p.is_file())


def test_pairing_binds_the_owner_once(api, config):
    pairing = Pairing(config)
    code = pairing.code()
    assert len(code) == 32 and re.fullmatch(r"[A-Za-z0-9_-]{32}", code)
    assert "https://t.me/pa_test_bot?start=" in pairing.out[0]
    stored = json.loads((config.state_dir / "telegram-pairing.json").read_text())
    assert set(stored) >= {"code_sha256", "expires_at"} and code not in json.dumps(stored)
    assert stat.S_IMODE((config.state_dir / "telegram-pairing.json").stat().st_mode) == 0o600
    api.push_message(text=f"/start {code}")
    assert pairing.join() == 0
    owner_file = config.state_dir / "telegram-owner.json"
    owner = json.loads(owner_file.read_text())
    assert owner["chat_id"] == owner["user_id"] == OWNER and owner["bot_id"] == BOT_ID
    assert stat.S_IMODE(owner_file.stat().st_mode) == 0o600
    assert not (config.state_dir / "telegram-pairing.json").exists()  # single use
    assert api.sent_texts(OWNER)[0].startswith("Paired.")
    assert pairing.out[-1] == "paired (chat …242)" and "request from user id …4242 (@owner_user)" in pairing.out
    assert code not in state_files(config) and TOKEN not in state_files(config)
    assert PairingStore(config.state_dir, BOT_ID).check(code, OWNER, time.time()) == "none"
    assert not PollLock(config.state_dir).held_elsewhere()  # the lock was released


def test_wrong_codes_get_no_reply_and_are_counted_per_sender(api, config, monkeypatch):
    monkeypatch.setattr(telegram, "PAIR_MAX_TOTAL_FAILURES", 7)
    pairing = Pairing(config)
    code = pairing.code()
    for _ in range(6):  # five count, the sixth from the same sender is ignored
        api.push_message(chat_id=1111, text="/start " + "x" * 32)
    time.sleep(0.6)
    data = json.loads((config.state_dir / "telegram-pairing.json").read_text())
    assert data["failures"] == {"1111": 5} and data["total_failures"] == 5
    api.push_message(chat_id=2222, text="/start wrongwrong")
    api.push_message(chat_id=2222, text="/start wrongagain")
    assert pairing.join() == 1
    assert "too many wrong codes" in pairing.err[-1]
    assert api.sent_texts() == [] and not (config.state_dir / "telegram-pairing.json").exists()
    assert code  # never used


def test_strangers_and_old_messages_are_ignored(api, config):
    pairing = Pairing(config)
    code = pairing.code()
    api.push_message(chat_id=-100, user_id=5, chat_type="group", text=f"/start {code}")
    api.push_message(chat_id=6, user_id=7, text=f"/start {code}")  # from.id != chat.id
    api.push_message(chat_id=8, is_bot=True, text=f"/start {code}")
    api.push_message(chat_id=9, date=int(time.time()) - 3600, text=f"/start {code}")  # backlog
    api.push_message(chat_id=10, text=f"/start@pa_test_bot {code}")
    assert pairing.join() == 0
    assert json.loads((config.state_dir / "telegram-owner.json").read_text())["chat_id"] == 10


def test_expiry_and_confirmation(api, config):
    now = [time.time()]

    def clock():
        now[0] += 200  # every look at the clock moves it on
        return now[0]

    pairing = Pairing(config, clock=clock)
    assert pairing.join() == 1 and "expired" in pairing.err[-1]
    assert not (config.state_dir / "telegram-pairing.json").exists()

    refused = Pairing(config, yes=False, confirm=lambda question: False)
    api.push_message(text=f"/start {refused.code()}")
    assert refused.join() == 1 and refused.err[-1] == "[not paired]"
    no_tty = Pairing(config, yes=False, confirm=lambda question: None)
    api.push_message(text=f"/start {no_tty.code()}")
    assert no_tty.join() == 1 and "--yes" in no_tty.err[-1]
    assert not (config.state_dir / "telegram-owner.json").exists()
    silent = []
    assert run_pairing(config, out=silent.append, err=silent.append, stdout_tty=lambda: False) == 2
    assert not any("start=" in line for line in silent)


def test_already_paired_lock_and_force(api, config):
    owners = OwnerStore(config.state_dir, BOT_ID)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    assert owners.save(telegram.Owner(BOT_ID, 555, 555, time.time()))
    errors = []
    assert run_pairing(config, out=errors.append, err=errors.append, print_code=True) == 1
    assert "already paired" in errors[-1]
    holder = PollLock(config.state_dir)
    assert holder.acquire()
    try:
        assert run_pairing(config, out=errors.append, err=errors.append, print_code=True, force=True) == 1
        assert "another process is polling this bot" in errors[-1]
    finally:
        holder.release()
    pairing = Pairing(config, force=True)
    api.push_message(text=f"/start {pairing.code()}")
    assert pairing.join() == 0
    assert any("paired to another chat" in t for t in api.sent_texts(555))


def test_cli_status_and_unpair_never_print_the_token(api, config, capsys):
    assert main(["telegram", "status"]) == 0
    out = capsys.readouterr().out
    assert TOKEN not in out and TOKEN.split(":")[1] not in out
    assert "bot id 123456789" in out and "@pa_test_bot" in out and "paired: no" in out and "webhook set: no" in out
    config.state_dir.mkdir(parents=True, exist_ok=True)
    OwnerStore(config.state_dir, BOT_ID).save(telegram.Owner(BOT_ID, 987654321, 987654321, time.time()))
    assert main(["telegram", "status", "--offline"]) == 0
    out = capsys.readouterr().out
    assert "paired: yes (chat …321" in out and "987654321" not in out and "notices brief: telegram" in out
    assert main(["telegram", "unpair"]) == 0
    assert "unpaired" in capsys.readouterr().out
    assert not (config.state_dir / "telegram-owner.json").exists()
    assert main(["telegram", "pair"]) == 2  # pytest's stdout is not a terminal: no code without --print-code
    assert "start=" not in capsys.readouterr().out


def test_cli_pair_end_to_end(api, config, capsys):
    result = []
    thread = threading.Thread(target=lambda: result.append(main(["telegram", "pair", "--print-code", "--yes"])),
                              daemon=True)
    thread.start()
    seen = ""
    code = None
    end = time.monotonic() + 5
    while code is None and time.monotonic() < end:
        seen += capsys.readouterr().out
        match = re.search(r"\?start=([A-Za-z0-9_-]{32}) ", seen)
        code = match.group(1) if match else None
        time.sleep(0.02)
    assert code
    api.push_message(text=f"/start {code}")
    thread.join(10)
    assert result == [0]


def test_owner_file_validation(config):
    config.state_dir.mkdir(parents=True, exist_ok=True)
    path = config.state_dir / "telegram-owner.json"
    owners = OwnerStore(config.state_dir, BOT_ID)
    owners.save(telegram.Owner(999, 5, 5, 1.0))
    assert owners.current() is None  # another bot's pairing
    path.write_text(json.dumps({"version": 1, "bot_id": BOT_ID, "chat_id": 5, "user_id": 6, "paired_at": 1}))
    assert owners.current() is None  # not a private chat
    path.write_text("{not json")
    assert owners.current() is None
    path.write_text(json.dumps({"version": 1, "bot_id": BOT_ID, "chat_id": 5, "user_id": 5, "paired_at": 1,
                                "pad": "x" * 2000}))
    assert owners.current() is None  # oversized
    path.unlink()
    target = config.state_dir.parent / "elsewhere.json"
    target.write_text(json.dumps({"version": 1, "bot_id": BOT_ID, "chat_id": 5, "user_id": 5, "paired_at": 1}))
    os.symlink(target, path)
    assert owners.current() is None  # a symlink is never followed
    path.unlink()
    owners.save(telegram.Owner(BOT_ID, 5, 5, 1.0))
    assert owners.current() == telegram.Owner(BOT_ID, 5, 5, 1.0)
    assert TelegramClient(TOKEN).bot_id == BOT_ID
    assert isinstance(Path(config.state_dir), Path)
