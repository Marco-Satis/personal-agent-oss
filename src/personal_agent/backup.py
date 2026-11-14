"""Backups of the state directory and the vault: one .tar.gz per run, checked, rotated.

`create_backup` packs the state directory and the vault (if it lives elsewhere) into
<backup dir>/pa-backup-YYYYmmdd-HHMMSS.tar.gz, written 0600 through a temporary file and
os.replace. It keeps the newest `keep` archives and removes older ones of its own name
pattern, nothing else.

Left out on purpose:
- the backup directory itself (no backup inside the next backup),
- the Claude Code config directory: it holds the CLI login (an OAuth token) and caches;
  after a restore you log in again with `claude setup-token`,
- runtime files: `run/` (sockets, worker pipes), `*.lock`, `*.tmp`, sockets and symlinks.

The archive still holds private data (notes, mail metadata, approvals, the Telegram pairing),
so it stays 0600 in a 0700 folder. A copy on the same disk does not protect against losing
the disk: copy the archives off the machine yourself.

`verify_backup` opens an archive and reads every member: only regular files and folders,
only relative names under state/ or vault/, nothing outside. Restoring is not automated yet.
"""

from __future__ import annotations

import os
import re
import stat
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

BACKUP_DIR_NAME = "backups"
DEFAULT_KEEP = 7
MAX_KEEP = 90
NAME_RE = re.compile(r"pa-backup-\d{8}-\d{6}\.tar\.gz")
SKIP_DIRS = frozenset({"run", BACKUP_DIR_NAME})
SKIP_SUFFIXES = (".lock", ".tmp")
ROOTS = ("state", "vault")


class BackupError(Exception):
    pass


@dataclass(frozen=True)
class BackupResult:
    path: Path
    files: int
    size: int
    removed: tuple[str, ...]


@dataclass(frozen=True)
class VerifyResult:
    files: int
    roots: tuple[str, ...]
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems and "state" in self.roots


def backup_dir(state_dir: Path, override: Path | None = None) -> Path:
    return Path(override) if override else Path(state_dir) / BACKUP_DIR_NAME


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _walk(root: Path, skip: list[Path]):
    """(path, relative name) of every regular file and folder under root; no symlinks, no skipped trees."""
    for current, dirs, files in os.walk(root, followlinks=False):
        here = Path(current)
        keep = []
        for name in sorted(dirs):
            sub = here / name
            if sub.is_symlink() or (here == root and name in SKIP_DIRS) or any(_inside(sub, s) for s in skip):
                continue
            keep.append(name)
            yield sub, sub.relative_to(root)
        dirs[:] = keep
        for name in sorted(files):
            path = here / name
            if name.endswith(SKIP_SUFFIXES):
                continue
            try:
                mode = os.lstat(path).st_mode
            except OSError:
                continue  # gone meanwhile
            if stat.S_ISREG(mode):
                yield path, path.relative_to(root)


def _filtered(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def create_backup(state_dir: Path, vault_dir: Path, *, claude_config_dir: Path | None = None,
                  dest: Path | None = None, keep: int = DEFAULT_KEEP, now: float | None = None) -> BackupResult:
    state_dir, vault_dir = Path(state_dir), Path(vault_dir)
    if not state_dir.is_dir():
        raise BackupError("the state directory does not exist")
    keep = max(1, min(int(keep), MAX_KEEP))
    target_dir = backup_dir(state_dir, dest)
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target_dir, 0o700)
    skip = [target_dir, claude_config_dir or state_dir / "claude-config"]
    sources = [("state", state_dir)]
    if vault_dir.is_dir() and not _inside(vault_dir, state_dir):
        sources.append(("vault", vault_dir))
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(time.time() if now is None else now))
    final = target_dir / f"pa-backup-{stamp}.tar.gz"
    if final.exists():
        raise BackupError(f"{final.name} exists already; try again in a second")
    tmp = target_dir / f".{final.name}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    files = 0
    try:
        with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as tar:
            for root_name, root in sources:
                tar.add(root, arcname=root_name, recursive=False, filter=_filtered)
                for path, rel in _walk(root, skip):
                    try:
                        tar.add(path, arcname=f"{root_name}/{rel.as_posix()}", recursive=False, filter=_filtered)
                    except OSError:
                        continue  # a file that vanished or became unreadable while packing
                    files += path.is_file()
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(tmp, final)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    removed = _rotate(target_dir, keep)
    return BackupResult(final, files, final.stat().st_size, removed)


def list_backups(target_dir: Path) -> list[Path]:
    """Archives of our own name pattern, oldest first."""
    try:
        return sorted(p for p in Path(target_dir).iterdir() if NAME_RE.fullmatch(p.name) and p.is_file())
    except OSError:
        return []


def _rotate(target_dir: Path, keep: int) -> tuple[str, ...]:
    removed = []
    for old in list_backups(target_dir)[:-keep]:
        try:
            old.unlink()
            removed.append(old.name)
        except OSError:
            pass
    return tuple(removed)


def verify_backup(path: Path) -> VerifyResult:
    problems: list[str] = []
    roots: set[str] = set()
    files = 0
    try:
        with tarfile.open(path, mode="r:gz") as tar:
            for member in tar:
                name = PurePosixPath(member.name)
                if name.is_absolute() or ".." in name.parts or not name.parts or name.parts[0] not in ROOTS:
                    problems.append(f"unexpected path in archive: {member.name[:80]!r}")
                    continue
                roots.add(name.parts[0])
                if member.isdir():
                    continue
                if not member.isreg():
                    problems.append(f"not a regular file: {member.name[:80]!r}")
                    continue
                handle = tar.extractfile(member)
                if handle is None:
                    problems.append(f"unreadable: {member.name[:80]!r}")
                    continue
                while handle.read(1 << 16):
                    pass
                files += 1
    except (OSError, tarfile.TarError, EOFError) as exc:
        problems.append(f"archive cannot be read ({type(exc).__name__})")
    if not problems and "state" not in roots:
        problems.append("the archive holds no state directory")
    return VerifyResult(files, tuple(sorted(roots)), tuple(problems))


__all__ = ["BackupError", "BackupResult", "VerifyResult", "backup_dir", "create_backup", "list_backups",
           "verify_backup"]
