"""M1: the worker's only write, `draft` (APPEND with \\Draft into the drafts folder)."""

import email
import time

import pytest

from fake_imap import FakeIMAP, Folder, Msg
from mail_helpers import PASSWORD, USER, account, certs, plain  # noqa: F401 - certs is a fixture
from personal_agent.mail_worker import APPEND_MODE, READ_MODE, ProtocolGuard, check_command, handle
from personal_agent.mailconfig import parse_connector_config

import json


def config_for(*accounts):
    return parse_connector_config(json.dumps({"version": 1, "accounts": list(accounts)}).encode())


@pytest.fixture
def server(certs):
    servers = []

    def start(modes=("drafts",), folders=None):
        srv = FakeIMAP(str(certs.cert), str(certs.key), password=PASSWORD, modes=set(modes),
                       folders=folders or {"INBOX": Folder("INBOX", messages=[Msg(1, plain())]),
                                           "Drafts": Folder("Drafts")})
        servers.append(srv)
        return srv

    yield start
    for srv in servers:
        srv.close()


def drafting(port, ca, **extra):
    return account(port=port, ca_file=ca, address=USER, drafts_folder="Drafts", allow_draft_append=True, **extra)


def draft(cfg, **fields):
    request = {"protocol": 1, "command": "draft", "deadline_s": 20, "account": "work",
               "to": "alice@example.org", "subject": "Re: Budget", "body": "Hallo Alice,\nanbei.\n", **fields}
    return handle(request, cfg, time.monotonic())


def test_draft_lands_in_the_drafts_folder_and_nothing_else_happens(server, certs):
    srv = server()
    result = draft(config_for(drafting(srv.port, certs.ca)), in_reply_to="<m1@example.org>")
    assert result["ok"] and result["folder"] == "Drafts"
    [(folder, flags, raw)] = srv.appended
    assert folder == "Drafts" and "\\Draft" in flags
    msg = email.message_from_bytes(raw)
    assert msg["From"] == USER and msg["To"] == "alice@example.org"
    assert msg["In-Reply-To"] == "<m1@example.org>" and "Hallo Alice" in msg.get_payload(decode=True).decode()
    assert srv.violations == []
    commands = {c.split()[0] for c in srv.commands}
    assert commands <= {"CAPABILITY", "LOGIN", "LIST", "APPEND", "LOGOUT"}


def test_draft_needs_the_account_switch(server, certs):
    srv = server()
    cfg = config_for(account(port=srv.port, ca_file=certs.ca))
    result = draft(cfg)
    assert not result["ok"] and result["errors"][0]["code"] == "append_disabled"
    assert srv.appended == [] and srv.commands == []  # never even connected


def test_missing_drafts_folder_is_reported(server, certs):
    srv = server(folders={"INBOX": Folder("INBOX")})
    result = draft(config_for(drafting(srv.port, certs.ca)))
    assert result["errors"][0]["code"] == "drafts_folder_missing" and srv.appended == []


@pytest.mark.parametrize("fields", [
    {"to": "not an address"}, {"to": "a@b.c\r\nBcc: evil@x.org"}, {"subject": 5},
    {"body": "x" * 20_001}, {"in_reply_to": "no brackets"}, {"account": "other"},
])
def test_bad_fields_are_refused_before_connecting(server, certs, fields):
    srv = server()
    result = draft(config_for(drafting(srv.port, certs.ca)), **fields)
    assert not result["ok"] and srv.commands == []


def test_header_injection_in_subject_is_flattened(server, certs):
    srv = server()
    assert draft(config_for(drafting(srv.port, certs.ca)), subject="Hi\r\nBcc: evil@x.org")["ok"]
    msg = email.message_from_bytes(srv.appended[0][2])
    assert msg["Bcc"] is None and "evil" in msg["Subject"]


def test_read_mode_still_refuses_append_and_list():
    for name in ("APPEND", "LIST"):
        with pytest.raises(ProtocolGuard):
            check_command(READ_MODE, name, ())
        check_command(APPEND_MODE, name, ())
    for name in ("STORE", "EXPUNGE", "SELECT", "DELETE"):
        with pytest.raises(ProtocolGuard):
            check_command(APPEND_MODE, name, ())
