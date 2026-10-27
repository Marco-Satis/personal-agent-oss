"""M3: voice messages through a local recognizer (stt.py) and the Telegram file download."""

import io
import json
import os
import stat
from pathlib import Path

import pytest

from personal_agent.config import Config, ConfigError
from personal_agent.stt import SttError, parse_command, transcribe
from personal_agent.telegram import TelegramClient, TelegramError

TOKEN = "123456789:" + "A" * 35  # pragma: allowlist secret


def script(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PA_STATE_DIR", str(tmp_path / "state"))
    for name in ("PA_STT_COMMAND", "PA_MODULES", "PA_VOICE_MAX_S", "PA_STT_TIMEOUT_S", "PA_FFMPEG"):
        monkeypatch.delenv(name, raising=False)
    # A fake ffmpeg: copies the input path's bytes to the output path (the last argument).
    ffmpeg = script(tmp_path / "ffmpeg", 'for a; do last="$a"; done\ncp "$5" "$last"\n')
    monkeypatch.setenv("PA_FFMPEG", str(ffmpeg))
    return monkeypatch


def test_command_must_be_absolute():
    assert parse_command("") is None
    assert parse_command("/opt/stt/run --lang de") == ("/opt/stt/run", "--lang", "de")
    with pytest.raises(ValueError):
        parse_command("whisper --lang de")


def test_config_gates_and_limits(env, tmp_path):
    assert Config.from_env().stt_command is None
    env.setenv("PA_STT_COMMAND", f"{tmp_path}/stt --lang de")
    assert Config.from_env().stt_command[1:] == ("--lang", "de")
    env.setenv("PA_MODULES", "telegram")
    assert Config.from_env().stt_command is None
    env.delenv("PA_MODULES")
    env.setenv("PA_STT_COMMAND", "stt")
    with pytest.raises(ConfigError, match="absolute"):
        Config.from_env()
    env.setenv("PA_STT_COMMAND", f"{tmp_path}/stt")
    env.setenv("PA_VOICE_MAX_S", "9999")
    with pytest.raises(ConfigError, match="PA_VOICE_MAX_S"):
        Config.from_env()


def test_transcribe_runs_ffmpeg_and_the_recognizer_with_a_clean_env(env, tmp_path):
    seen = tmp_path / "seen.json"
    stt = script(tmp_path / "stt", f'env > "{seen}.env"\nprintf "Termin\\033[31m morgen um 9 "\n')
    env.setenv("PA_STT_COMMAND", str(stt))
    env.setenv("ANTHROPIC_API_KEY", "must-not-leak")  # pragma: allowlist secret
    text = transcribe(b"OggS fake audio", Config.from_env())
    assert text.startswith("Termin") and text.endswith("morgen um 9") and "\x1b" not in text
    assert "must-not-leak" not in Path(f"{seen}.env").read_text()
    assert list((tmp_path / "state" / "run").iterdir()) == []  # temp files removed


@pytest.mark.parametrize("body,reason", [("exit 3\n", "failed"), ("sleep 5\n", "timeout"), ("printf '  '\n", "empty")])
def test_transcribe_failures_have_fixed_reasons(env, tmp_path, body, reason):
    env.setenv("PA_STT_COMMAND", str(script(tmp_path / "stt", body)))
    env.setenv("PA_STT_TIMEOUT_S", "10")
    config = Config.from_env()
    if reason == "timeout":
        import dataclasses
        config = dataclasses.replace(config, stt_timeout_s=1)
    with pytest.raises(SttError, match=reason):
        transcribe(b"audio", config)


def test_transcribe_refuses_oversized_audio(env, tmp_path):
    env.setenv("PA_STT_COMMAND", str(script(tmp_path / "stt", "echo hi\n")))
    with pytest.raises(SttError, match="too_large"):
        transcribe(b"x" * (2 * 1024 * 1024 + 1), Config.from_env())


class Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opener_for(file_path, data, size=None):
    calls = []

    def opener(request, timeout=None):
        calls.append(request.full_url)
        if request.full_url.endswith("/getFile"):
            result = {"file_id": "f1", "file_path": file_path}
            if size is not None:
                result["file_size"] = size
            return Response(json.dumps({"ok": True, "result": result}).encode())
        return Response(data)

    return opener, calls


def test_download_fetches_the_file(tmp_path):
    opener, calls = opener_for("voice/file_1.oga", b"OggS")
    client = TelegramClient(TOKEN, opener=opener, sleep=lambda s: None)
    assert client.download("f1", 1024) == b"OggS"
    assert calls[1].endswith("/file/bot" + TOKEN + "/voice/file_1.oga")


@pytest.mark.parametrize("path,size,data", [("../etc/passwd", None, b"x"), ("voice/a.oga", 10_000, b"x"),
                                             ("voice/a.oga", None, b"x" * 2000)])
def test_download_refuses_bad_paths_and_big_files(path, size, data):
    opener, _ = opener_for(path, data, size)
    client = TelegramClient(TOKEN, opener=opener, sleep=lambda s: None)
    with pytest.raises(TelegramError):
        client.download("f1", 1024)
