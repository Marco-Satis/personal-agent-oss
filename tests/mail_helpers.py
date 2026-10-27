"""Shared pieces for the mail tests: test-only TLS certificates, a mail corpus, config files.

The certificates are generated at test time with `openssl` into a temporary directory
(nothing key-like is committed); without openssl the TLS integration tests are skipped.
Clients trust the test CA through the `ca_file` key, so certificate verification stays
on in every test.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import replace
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace

import pytest

from personal_agent.config import Config

PASSWORD = "S3cret-Pass-for-tests-9f8e7d"  # seeded secret: must never appear in any output  # pragma: allowlist secret  # gitleaks:allow
USER = "me@example.org"


def _openssl(*args: str, cwd: Path) -> None:
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True, timeout=60)


def make_certs(directory: Path) -> SimpleNamespace:
    """A test-only CA, a server certificate for localhost/127.0.0.1 signed by it, one for
    another host name, and an untrusted self-signed certificate."""
    if shutil.which("openssl") is None:
        pytest.skip("openssl is not available")
    directory.mkdir(parents=True, exist_ok=True)
    ec = ("-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes")
    _openssl("req", "-x509", *ec, "-keyout", "ca.key", "-out", "ca.pem", "-days", "36500",
             "-subj", "/CN=personal-agent test-only CA", "-addext", "basicConstraints=critical,CA:TRUE",
             "-addext", "keyUsage=critical,keyCertSign,cRLSign", cwd=directory)
    for name, san in (("server", "DNS:localhost,IP:127.0.0.1"), ("other", "DNS:other.test")):
        (directory / f"{name}.ext").write_text(
            f"subjectAltName={san}\nbasicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature\n"
            "extendedKeyUsage=serverAuth\nauthorityKeyIdentifier=keyid\nsubjectKeyIdentifier=hash\n")
        _openssl("req", *ec, "-keyout", f"{name}.key", "-out", f"{name}.csr", "-subj", "/CN=test-only", cwd=directory)
        _openssl("x509", "-req", "-in", f"{name}.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
                 "-out", f"{name}.pem", "-days", "36500", "-extfile", f"{name}.ext", cwd=directory)
    _openssl("req", "-x509", *ec, "-keyout", "untrusted.key", "-out", "untrusted.pem", "-days", "36500",
             "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1", cwd=directory)
    for path in directory.iterdir():
        path.chmod(0o600)
    return SimpleNamespace(dir=directory, ca=directory / "ca.pem", cert=directory / "server.pem",
                           key=directory / "server.key", other_cert=directory / "other.pem",
                           other_key=directory / "other.key", untrusted_cert=directory / "untrusted.pem",
                           untrusted_key=directory / "untrusted.key")


@pytest.fixture(scope="session")
def certs(tmp_path_factory) -> SimpleNamespace:
    return make_certs(tmp_path_factory.mktemp("test-only-certs"))


# --- config files ------------------------------------------------------------------------


def account(name: str = "work", port: int = 993, *, ca_file: Path | None = None, folders=("INBOX",),
            password: str = PASSWORD, **extra) -> dict:
    data = {"name": name, "host": "127.0.0.1", "port": port, "user": USER, "password": password,
            "folders": list(folders), "ca_file": str(ca_file) if ca_file else None}
    data.update(extra)
    return data


def write_config(path: Path, accounts: list[dict], calendars: list[dict] | None = None, mode: int = 0o600,
                 raw: str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = raw if raw is not None else json.dumps({"version": 1, "accounts": accounts,
                                                    "calendars": calendars or []})
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    path.chmod(mode)
    return path


def agent_config(tmp_path: Path, mail_config: Path | None = None, **overrides) -> Config:
    base = Config(state_dir=tmp_path / "state", status_dir=tmp_path / "sessions", vault_dir=tmp_path / "vault",
                  claude_config_dir=tmp_path / "claude-config", mail_config=mail_config, router_small_model=None)
    return replace(base, **overrides)


# --- the mail corpus ---------------------------------------------------------------------


def plain(subject: str = "Re: Budget", body: str = "Hello,\nplease check the numbers.\n",
          sender: str = "Alice Example <alice@example.org>", **headers: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = USER
    msg["Subject"] = subject
    msg["Date"] = "Mon, 19 Oct 2026 08:12:00 +0200"
    msg["Message-ID"] = "<m1@example.org>"
    for name, value in headers.items():
        msg[name.replace("_", "-")] = value
    msg.set_content(body)
    return bytes(msg)


def html_only(html: str, subject: str = "News", sender: str = "Example Shop <news@shop.example>") -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = USER
    msg["Subject"] = subject
    msg.set_content(html, subtype="html")
    return bytes(msg)


def alternative(text: str, html: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = "Bob <bob@example.net>"
    msg["Subject"] = "alt"
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return bytes(msg)


def with_attachments() -> bytes:
    msg = EmailMessage()
    msg["From"] = "Carol <carol@example.com>"
    msg["Subject"] = "files"
    msg.set_content("see attached")
    msg.add_attachment(b"%PDF-1.4 secret attachment content", maintype="application", subtype="pdf",
                       filename="report.pdf")
    inner = EmailMessage()
    inner["From"] = "x@y.org"
    inner["Subject"] = "inner"
    inner.set_content("INNER MESSAGE TEXT must not be inlined")
    msg.add_attachment(inner)
    return bytes(msg)


def raw_mail(headers: str, body: bytes) -> bytes:
    return headers.replace("\n", "\r\n").encode("utf-8") + b"\r\n\r\n" + body
