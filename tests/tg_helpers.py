"""Shared setup for the serve-side Telegram tests: a fake Bot API, fake_claude, a paired owner."""

from __future__ import annotations

import json
import shlex
import socket
import stat
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from fake_telegram import BOT_ID, OWNER, TOKEN, FakeBotAPI
from personal_agent import telegram
from personal_agent.config import Config
from personal_agent.notify import Notifier
from personal_agent.server import Agent
from personal_agent.telegram import Owner, OwnerStore
from personal_agent.telegram_bot import TelegramService
from personal_agent.telephony import FakeProvider

FAKE_CLAUDE = Path(__file__).resolve().parent / "fake_claude.py"
PIN = "731946"
CALL_PIN = "482913"
API_TOKEN = "t" * 32


def block_network(monkeypatch) -> None:
    real = socket.socket.connect

    def guarded(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and host not in ("127.0.0.1", "::1", "localhost"):
            raise OSError("network disabled in tests")
        return real(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


def claude_wrapper(tmp_path: Path) -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "fake.jsonl"
    wrapper = tmp_path / "claude"
    wrapper.write_text(f"#!/bin/sh\nFAKE_CLAUDE_LOG={shlex.quote(str(log))} exec "
                       f"{shlex.quote(sys.executable)} {shlex.quote(str(FAKE_CLAUDE))} \"$@\"\n")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return wrapper, log


def make_config(tmp_path: Path, **overrides) -> Config:
    wrapper, _ = claude_wrapper(tmp_path)
    base = Config(
        state_dir=tmp_path / "state",
        status_dir=tmp_path / "sessions",
        port=0,
        api_token=API_TOKEN,
        call_to="+447700900123",
        call_pin=CALL_PIN,
        approval_pin=PIN,
        telegram_token=TOKEN,
        claude_bin=str(wrapper),
        router_small_model=None,
        vault_dir=tmp_path / "vault",
        claude_config_dir=tmp_path / "claude-config",
        approval_timeout_s=60,
        min_call_interval_min=0,
    )
    return replace(base, **overrides)


def pair(config: Config, chat_id: int = OWNER) -> None:
    config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    assert OwnerStore(config.state_dir, BOT_ID).save(Owner(BOT_ID, chat_id, chat_id, time.time()))


def start_bot(tmp_path: Path, monkeypatch, paired: bool = True, api: FakeBotAPI | None = None, **overrides):
    """A running TelegramService (fast ticks) with an Agent and its inbox. Call .close() at the end."""
    monkeypatch.setattr(telegram, "SEND_GAP_S", 0.0)
    fake = api or FakeBotAPI(max_poll_s=0.2)
    monkeypatch.setattr(telegram, "DEFAULT_API_BASE", fake.base)
    config = make_config(tmp_path, **overrides)
    if paired:
        pair(config)
    notifier = Notifier.from_config(config)
    service = TelegramService(config, notifier, tick_s=0.1, poll_timeout_s=1)
    notifier.targets["telegram"] = service.target
    agent = Agent(config, provider=FakeProvider(), notifier=notifier, telegram=service)
    service.start(agent.approvals)
    log = tmp_path / "fake.jsonl"

    def starts() -> list[dict]:
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines() if '"args"' in line]

    def close() -> None:
        service.close()
        notifier.close(2.0)
        fake.close()

    def wait_polling(timeout: float = 5.0) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end and not service.polling:
            time.sleep(0.02)
        assert service.polling

    return SimpleNamespace(api=fake, config=config, service=service, agent=agent, inbox=agent.approvals,
                           notifier=notifier, log=log, starts=starts, close=close, wait_polling=wait_polling,
                           vault=config.vault_path)


def wait_text(api: FakeBotAPI, needle: str, timeout: float = 10.0, chat_id: int = OWNER) -> str:
    body = api.wait_for("sendMessage", lambda b: b.get("chat_id") == chat_id and needle in b.get("text", ""),
                        timeout=timeout)
    return body["text"]


def wait_until(predicate, timeout: float = 5.0, step: float = 0.02) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(step)
    return bool(predicate())
