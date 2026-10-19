"""Small private JSON files in the state directory (brief state, chunk lists, ...).

Writes are atomic: a unique temp file in the same directory, mode 0600, fsync, then
os.replace. Reads refuse symbolic links (O_NOFOLLOW), anything that is not a regular
file owned by this user, files over a size cap and JSON objects of another version.
Every failure is reported as "missing", never as an exception.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def write_private_json(directory: Path, name: str, payload: dict, *, prefix: str = ".state-") -> bool:
    """Write directory/<name> atomically (unique temp file, 0600). False on any error."""
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    tmp: str | None = None
    try:
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=prefix, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, Path(directory) / name)
        tmp = None
        return True
    except OSError as exc:
        log.warning("could not save %s: %s", name, type(exc).__name__)
        return False
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def read_private_json(directory: Path, name: str, cap: int, version: int | tuple[int, ...]) -> dict | None:
    """directory/<name> as a JSON object of the given version(s), or None (missing or invalid)."""
    versions = (version,) if isinstance(version, int) else tuple(version)
    try:
        fd = os.open(Path(directory) / name, _READ_FLAGS)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > cap:
            return None
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            return None
        raw = os.read(fd, cap + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(raw) > cap:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    if not isinstance(data, dict) or data.get("version") not in versions or isinstance(data.get("version"), bool):
        return None
    return data


def stat_key(directory: Path, name: str) -> tuple[int, int, int] | None:
    """(mtime_ns, size, inode) of directory/<name> without following a link; None when missing.

    Caches of files that other processes write compare this key and re-read on a change.
    """
    try:
        info = os.stat(Path(directory) / name, follow_symlinks=False)
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size, info.st_ino


def remove_private(directory: Path, name: str) -> None:
    try:
        os.unlink(Path(directory) / name)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("could not remove %s: %s", name, type(exc).__name__)
