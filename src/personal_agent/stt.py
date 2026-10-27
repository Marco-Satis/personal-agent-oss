"""Speech to text for Telegram voice messages, through a local program of your choice.

The package stays stdlib-only: the recognizer is an external command (PA_STT_COMMAND,
an absolute path plus fixed arguments), for example whisper.cpp or the bundled
scripts/stt_faster_whisper.py in its own virtualenv. The audio never leaves the machine.

Steps: the voice file (Telegram sends Ogg/Opus) is written to a private temp folder,
converted by ffmpeg to 16 kHz mono WAV, and the command is run with the WAV path as its
last argument. Its stdout is the transcript. Both programs get a minimal environment
(no tokens), a time limit, and capped output; the temp files are removed afterwards.

The transcript is untrusted text like any typed message: it goes through the same
router, approvals and guards. A transcript that looks like a PIN is never used.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from .status import clean_text

if TYPE_CHECKING:
    from .config import Config

MAX_AUDIO_BYTES = 2 * 1024 * 1024
MAX_TRANSCRIPT_CHARS = 2000
MAX_OUTPUT_BYTES = 64 * 1024
ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "TZ")


class SttError(RuntimeError):
    """A fixed, safe reason (never a path or program output)."""


def parse_command(raw: str | None) -> tuple[str, ...] | None:
    """PA_STT_COMMAND split like a shell would; the program must be an absolute path."""
    if raw is None or not raw.strip():
        return None
    parts = tuple(shlex.split(raw))
    if not parts or not os.path.isabs(parts[0]):
        raise ValueError("PA_STT_COMMAND must start with an absolute path to the program")
    return parts


def _env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k in ENV_KEEP}


def _run(argv: list[str], timeout_s: float) -> bytes:
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              env=_env(), timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired:
        raise SttError("timeout") from None
    except OSError:
        raise SttError("unavailable") from None
    if done.returncode != 0:
        raise SttError("failed")
    return done.stdout[:MAX_OUTPUT_BYTES]


def transcribe(audio: bytes, config: "Config") -> str:
    """The transcript of one voice message (cleaned, one block). Raises SttError."""
    command = config.stt_command
    if command is None:
        raise SttError("off")
    if not audio or len(audio) > MAX_AUDIO_BYTES:
        raise SttError("too_large")
    run_dir = Path(config.state_dir) / "run"
    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(dir=run_dir, prefix="stt-") as tmp:
        os.chmod(tmp, 0o700)
        source, wav = Path(tmp) / "voice.oga", Path(tmp) / "voice.wav"
        fd = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(audio)
        _run([config.ffmpeg_bin, "-nostdin", "-loglevel", "error", "-i", str(source), "-ar", "16000", "-ac", "1",
              "-t", str(config.voice_max_s), str(wav)], 60)
        raw = _run([*command, str(wav)], config.stt_timeout_s)
    text = clean_text(raw.decode("utf-8", "replace"), MAX_TRANSCRIPT_CHARS).strip()
    if not text:
        raise SttError("empty")
    return text


__all__ = ["SttError", "parse_command", "transcribe"]
