#!/usr/bin/env python3
"""Recognizer for PA_STT_COMMAND: prints the transcript of one WAV file to stdout.

Runs in its own virtualenv with faster-whisper installed (the agent itself stays
stdlib-only). Example:

    python3 -m venv ~/.local/share/personal-agent/stt-venv
    ~/.local/share/personal-agent/stt-venv/bin/pip install faster-whisper
    PA_STT_COMMAND="$HOME/.local/share/personal-agent/stt-venv/bin/python /path/to/stt_faster_whisper.py --lang de"

The model is downloaded once on first use (set --model-dir to keep it in a fixed
place; after that the script works offline). CPU only, int8.
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("wav")
    parser.add_argument("--lang", default=None, help="language code such as de; default: detect")
    parser.add_argument("--model", default="small", help="tiny, base, small, medium, ... (default: small)")
    parser.add_argument("--model-dir", default=None, help="where models are stored")
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args(argv)

    from faster_whisper import WhisperModel

    options = dict(device="cpu", compute_type="int8", cpu_threads=args.threads, download_root=args.model_dir)
    try:  # no network once the model is on disk
        model = WhisperModel(args.model, local_files_only=True, **options)
    except Exception:  # noqa: BLE001 - first use: download it
        model = WhisperModel(args.model, **options)
    segments, _ = model.transcribe(args.wav, language=args.lang, beam_size=1, vad_filter=True)
    text = " ".join(s.text.strip() for s in segments).strip()
    if not text:
        return 1
    sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
