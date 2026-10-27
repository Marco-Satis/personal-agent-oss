"""AP4.1: the connector config file (mail passwords) and its strict validation."""

import json
import os

import pytest

from mail_helpers import PASSWORD, account, write_config
from personal_agent import mailconfig
from personal_agent.mailconfig import (
    MailConfigError,
    file_problem,
    imap_utf7,
    load_connector_config,
    parse_connector_config,
    quote_astring,
    quote_mailbox,
)

URL_TOKEN = "private-0123abcdTOKEN"


def doc(accounts=None, calendars=None, **top):
    data = {"version": 1, "accounts": [account()] if accounts is None else accounts}
    if calendars is not None:
        data["calendars"] = calendars
    data.update(top)
    return json.dumps(data).encode()


def error_of(raw: bytes) -> str:
    with pytest.raises(MailConfigError) as info:
        parse_connector_config(raw)
    message = str(info.value)
    assert PASSWORD not in message and URL_TOKEN not in message
    return message


def test_valid_file_and_defaults(tmp_path):
    path = write_config(tmp_path / "mail.json", [account(folders=("INBOX", "Entwürfe"), address="me@example.org",
                                                       drafts_folder="Drafts")],
                        [{"name": "personal", "url": f"webcal://cal.example.org/{URL_TOKEN}/basic.ics"}])
    config = load_connector_config(path)
    work = config.accounts[0]
    assert work.name == "work" and work.port == 993 and work.folders == ("INBOX", "Entwürfe")
    assert work.password == PASSWORD and work.allow_draft_append is False
    assert config.calendars[0].url == f"https://cal.example.org/{URL_TOKEN}/basic.ics"  # webcal rewritten
    shown = repr(config)
    assert PASSWORD not in shown and "me@example.org" not in shown and URL_TOKEN not in shown
    assert config.account("work") is work and config.account("nope") is None


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660])
def test_group_or_other_bits_are_refused(tmp_path, mode):
    path = write_config(tmp_path / "mail.json", [account()], mode=mode)
    assert file_problem(path) == mailconfig.FILE_PROBLEM
    with pytest.raises(MailConfigError, match="0600"):
        load_connector_config(path)


def test_symlink_missing_and_other_owner(tmp_path, monkeypatch):
    real = write_config(tmp_path / "real.json", [account()])
    link = tmp_path / "link.json"
    link.symlink_to(real)
    assert file_problem(link) == mailconfig.FILE_PROBLEM
    with pytest.raises(MailConfigError):
        load_connector_config(link)
    assert "does not exist" in file_problem(tmp_path / "missing.json")
    assert file_problem(None) == "mail is not configured (PA_MAIL_CONFIG)"
    monkeypatch.setattr(os, "getuid", lambda: os.stat(real).st_uid + 1)
    assert file_problem(real) == mailconfig.FILE_PROBLEM
    with pytest.raises(MailConfigError):
        load_connector_config(real)


def test_file_problem_never_opens_the_file(tmp_path, monkeypatch):
    path = write_config(tmp_path / "mail.json", [account()])

    def no_open(*a, **k):
        raise AssertionError("file_problem must not open the file")

    monkeypatch.setattr(os, "open", no_open)
    monkeypatch.setattr("builtins.open", no_open)
    assert file_problem(path) is None


def test_oversized_file_is_refused(tmp_path):
    path = write_config(tmp_path / "mail.json", [], raw="{" + " " * (mailconfig.MAX_CONFIG_BYTES + 10) + "}")
    assert "too large" in file_problem(path)
    with pytest.raises(MailConfigError, match="too large"):
        load_connector_config(path)


def test_duplicate_and_unknown_keys():
    assert "duplicate" in error_of(b'{"version": 1, "version": 1, "accounts": []}')
    raw = json.dumps({"version": 1, "accounts": [dict(account(), pasword="x")]}).encode()
    assert "unknown key (pasword)" in error_of(raw)
    assert "unknown key" in error_of(doc(extra=1))
    assert "not valid JSON" in error_of(b"{nope")


@pytest.mark.parametrize("field, value, needle", [
    ("name", "Work", "accounts[0].name"),
    ("name", "a" * 25, "accounts[0].name"),
    ("host", "https://imap.example.org", "accounts[0].host"),
    ("host", "imap.example.org/x", "accounts[0].host"),
    ("host", "user@imap.example.org", "accounts[0].host"),
    ("port", 0, "accounts[0].port"),
    ("port", "993", "accounts[0].port"),
    ("port", True, "accounts[0].port"),
    ("user", "jörg@example.org", "accounts[0].user"),
    ("user", "a\r\nb", "accounts[0].user"),
    ("password", "pässwort", "use an app password"),
    ("password", "", "accounts[0].password"),
    ("folders", [], "accounts[0].folders"),
    ("folders", ["INBOX", "INBOX"], "twice"),
    ("folders", ["a\nb"], "control characters"),
    ("folders", ["x" * 101], "folders[0]"),
    ("address", "Alice <a@example.org>", "accounts[0].address"),
    ("allow_draft_append", "yes", "allow_draft_append"),
    ("ca_file", "relative/ca.pem", "ca_file"),
])
def test_field_rules_name_the_key_never_the_value(field, value, needle):
    message = error_of(json.dumps({"version": 1, "accounts": [dict(account(), **{field: value})]}).encode())
    assert needle in message
    if isinstance(value, str) and len(value) > 3:
        assert value not in message


def test_draft_append_needs_address_and_folder_and_names_are_unique():
    assert "needs address" in error_of(doc([account(allow_draft_append=True)]))
    assert "same name" in error_of(doc([account(), account()]))
    assert "at most 5" in error_of(doc([account(name=f"a{i}") for i in range(6)]))
    assert "no account and no calendar" in error_of(doc([]))
    assert "version must be 1" in error_of(doc(version=2))


@pytest.mark.parametrize("url, needle", [
    (f"http://cal.example.org/{URL_TOKEN}", "must use https"),
    (f"https://user:pw@cal.example.org/{URL_TOKEN}", "no user or password"),  # pragma: allowlist secret
    (f"https://cal.example.org/{URL_TOKEN}#frag", "fragment"),
    (f"ftp://cal.example.org/{URL_TOKEN}", "must use https"),
    (f"https:///{URL_TOKEN}", "must name a host"),
    (f"https://cal.example.org/{URL_TOKEN} x", "spaces"),
])
def test_calendar_urls(url, needle):
    assert needle in error_of(doc([], [{"name": "cal", "url": url}]))


def test_imap_names_are_encoded_and_quoted():
    assert imap_utf7("INBOX") == "INBOX"
    assert imap_utf7("Entwürfe") == "Entw&APw-rfe"
    assert imap_utf7("台北") == "&U,BTFw-"  # RFC 3501 example
    assert imap_utf7("A&B") == "A&-B"
    assert quote_mailbox('a"b\\c') == '"a\\"b\\\\c"'
    assert quote_astring("me@example.org") == '"me@example.org"'
    with pytest.raises(MailConfigError):
        quote_astring("ä")
