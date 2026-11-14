"""B10: backups of state and vault (backup.py, `personal-agent backup`)."""

import io
import os
import socket
import stat
import tarfile
import time

import pytest

from personal_agent.backup import BackupError, create_backup, list_backups, verify_backup
from personal_agent.cli import main

NOW = time.mktime((2026, 11, 14, 9, 0, 0, 0, 0, -1))


def fill(state, vault=None):
    (state / "wiki").mkdir(parents=True)
    (state / "escalation.json").write_text("{}")
    (state / "wiki" / "note.md").write_text("hello")
    (state / "run").mkdir()
    (state / "run" / "worker.pipe").write_text("x")
    (state / "mail.lock").write_text("")
    (state / "x.json.tmp").write_text("")
    (state / "claude-config").mkdir()
    (state / "claude-config" / ".credentials.json").write_text("secret-login")  # pragma: allowlist secret
    (state / "link").symlink_to("/etc/hostname")
    if vault is not None:
        vault.mkdir()
        (vault / "heute.md").write_text("# heute")


def names(path):
    with tarfile.open(path) as tar:
        return sorted(tar.getnames())


def test_backup_packs_state_and_outside_vault_without_secrets_and_runtime_files(tmp_path):
    state, vault = tmp_path / "state", tmp_path / "vault"
    fill(state, vault)
    result = create_backup(state, vault, now=NOW)
    assert result.path.name == "pa-backup-20261114-090000.tar.gz"
    assert names(result.path) == ["state", "state/escalation.json", "state/wiki", "state/wiki/note.md",
                                  "vault", "vault/heute.md"]
    assert stat.S_IMODE(os.stat(result.path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(result.path.parent).st_mode) == 0o700
    assert result.files == 3
    check = verify_backup(result.path)
    assert check.ok and check.files == 3 and check.roots == ("state", "vault")
    with tarfile.open(result.path) as tar:
        assert all(m.uid == 0 and m.uname == "" for m in tar)


def test_vault_inside_state_is_not_packed_twice_and_backups_are_not_nested(tmp_path):
    state = tmp_path / "state"
    fill(state)
    (state / "vault").mkdir()
    (state / "vault" / "heute.md").write_text("x")
    first = create_backup(state, state / "vault", now=NOW)
    second = create_backup(state, state / "vault", now=NOW + 1)
    assert "state/vault/heute.md" in names(second.path)
    assert not any(n.startswith(("vault", "state/backups")) for n in names(second.path))
    assert list_backups(first.path.parent) == [first.path, second.path]


def test_rotation_keeps_the_newest_and_only_touches_own_archives(tmp_path):
    state = tmp_path / "state"
    fill(state)
    other = state / "backups" / "keep-me.tar.gz"
    other.parent.mkdir(mode=0o700)
    other.write_text("not ours")
    for i in range(4):
        result = create_backup(state, tmp_path / "none", keep=2, now=NOW + i)
    assert [p.name for p in list_backups(state / "backups")] == ["pa-backup-20261114-090002.tar.gz",
                                                                  "pa-backup-20261114-090003.tar.gz"]
    assert result.removed == ("pa-backup-20261114-090001.tar.gz",)
    assert other.exists()


def test_same_second_does_not_overwrite_and_leaves_no_temp_file(tmp_path):
    state = tmp_path / "state"
    fill(state)
    create_backup(state, tmp_path / "none", now=NOW)
    with pytest.raises(BackupError):
        create_backup(state, tmp_path / "none", now=NOW)
    assert not [p for p in (state / "backups").iterdir() if p.name.endswith(".tmp")]


def test_missing_state_dir_is_an_error(tmp_path):
    with pytest.raises(BackupError):
        create_backup(tmp_path / "nope", tmp_path / "vault")


def _archive(path, members):
    with tarfile.open(path, "w:gz") as tar:
        for name, kind in members:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size = 2
                tar.addfile(info, io.BytesIO(b"ok"))
            elif kind == "link":
                info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
                tar.addfile(info)


def test_verify_flags_foreign_paths_links_and_broken_archives(tmp_path):
    bad = tmp_path / "bad.tar.gz"
    _archive(bad, [("state/a", "file"), ("../evil", "file"), ("/abs", "file"), ("state/l", "link")])
    check = verify_backup(bad)
    assert not check.ok and len(check.problems) == 3
    empty = tmp_path / "empty.tar.gz"
    _archive(empty, [("vault/a", "file")])
    assert verify_backup(empty).problems == ("the archive holds no state directory",)
    broken = tmp_path / "broken.tar.gz"
    broken.write_bytes(b"\x1f\x8b not really gzip")
    assert not verify_backup(broken).ok


def test_cli_create_list_verify(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    fill(state)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PA_STATE_DIR", str(state))
    monkeypatch.delenv("PA_VAULT_DIR", raising=False)
    assert main(["backup", "create", "--keep", "3"]) == 0
    out = capsys.readouterr().out
    assert "check: ok" in out and "not in the backup" in out
    assert main(["backup", "list"]) == 0
    assert "pa-backup-" in capsys.readouterr().out
    assert main(["backup", "verify"]) == 0
    assert "ok" in capsys.readouterr().out


def test_sockets_are_skipped(tmp_path):
    state = tmp_path / "state"
    fill(state)
    sock = socket.socket(socket.AF_UNIX)
    path = state / "s.sock"
    if len(str(path)) > 100:
        pytest.skip("path too long for a unix socket")
    sock.bind(str(path))
    try:
        result = create_backup(state, tmp_path / "none", now=NOW)
    finally:
        sock.close()
    assert "state/s.sock" not in names(result.path)
