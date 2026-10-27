"""M1: MailService with a stub worker: overview, categories, health, show, payment check, draft, trust."""

import os
import time
from pathlib import Path

import pytest

from personal_agent.config import Config
from personal_agent.mail import MailCache, MailService, WorkerError, parse_mail_command
from personal_agent.mailtriage import (PayeeStore, category_counts, classify, find_ibans, health_notes, mask_iban,
                                       payee_lines)

NOW = time.mktime((2026, 10, 28, 9, 0, 0, 0, 0, -1))  # a Wednesday
IBAN_A = "DE89370400440532013000"  # textbook example IBAN, valid check digits
IBAN_B = "DE02120300000000202051"  # pragma: allowlist secret


class StubWorker:
    def __init__(self, scan=None, body=None, draft=None):
        self.scan, self.body, self.draft_result = scan, body, draft
        self.calls = []

    def call(self, command, payload, timeout_s=None):
        self.calls.append((command, payload))
        if command == "scan":
            return self.scan
        if command == "bodies":
            item = payload["items"][0]
            return {"ok": True, "items": [{"ref_key": [item["account"], item["folder"], item["uidvalidity"],
                                                       item["uid"]], **self.body}]}
        if command == "draft":
            if isinstance(self.draft_result, Exception):
                raise self.draft_result
            return self.draft_result
        raise AssertionError(command)


def record(uid, subject, sender="alice@example.org", name="Alice", seen=False, lst=False, date=NOW - 3600):
    return {"uid": uid, "date": date, "flags": {"seen": seen, "answered": False}, "size": 100,
            "from_name": name, "from_addr": sender, "subject": subject, "list": lst, "auto": False}


def scan_response(records, error=None):
    folder = {"uidvalidity": 7, "uidnext": 99, "messages": len(records), "unseen": len(records), "records": records}
    return {"ok": True, "errors": [], "accounts": [{"name": "work", "error": error, "folders": {"INBOX": folder}}]}


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PA_VAULT_DIR", str(tmp_path / "vault"))
    conn = tmp_path / "secret" / "mail.json"
    conn.parent.mkdir()
    conn.write_text("{}")
    os.chmod(conn, 0o600)
    monkeypatch.setenv("PA_MAIL_CONFIG", str(conn))
    monkeypatch.setenv("PA_MAIL_TELEGRAM", "full")
    for name in ("PA_LANGUAGE", "PA_MODULES"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "state").mkdir()
    (tmp_path / "vault").mkdir()
    return Config.from_env()


def service(config, worker, clock=lambda: NOW):
    return MailService(config, worker, MailCache(config.state_dir, clock=clock), clock=clock,
                       since=lambda: NOW - 7200)


def first_ref(svc):
    return svc.cache.load().messages()[0].ref


# --- pure triage -----------------------------------------------------------------------------


@pytest.mark.parametrize("subject,flags,expected", [
    ("Mahnung zur Rechnung 4711", {}, "deadline"),
    ("Ihre Rechnung Oktober", {}, "payment"),
    ("Reklamation: Bremse defekt", {}, "complaint"),
    ("Anfrage Kostenvoranschlag E-Bike", {}, "request"),
    ("Ihr Sicherheitscode", {}, "confidential"),
    ("Unser Angebot der Woche", {"is_list": True}, "automated"),
    ("Hallo zusammen", {}, "other"),
    ("Kurz Bescheid wegen morgen", {}, "other"),
    ("Invoice #12 overdue", {}, "deadline"),
])
def test_classify(subject, flags, expected):
    assert classify(subject, **flags) == expected


def test_category_counts_keep_order_and_drop_zeros():
    counts = category_counts([("Rechnung 1", False, False), ("Frist läuft", False, False), ("Rechnung 2", False, False)])
    assert counts == [("deadline", 1), ("payment", 2)]


def test_ibans_are_found_validated_and_masked():
    text = f"Bitte auf DE89 3704 0044 0532 0130 00 überweisen. Falsch: DE89370400440532013001. {IBAN_B}"
    assert find_ibans(text) == [IBAN_A, IBAN_B]
    assert mask_iban(IBAN_A) == "DE89 **** 3000"


def test_payee_store_new_known_changed_and_trust(tmp_path):
    store = PayeeStore(tmp_path)
    assert [c.status for c in store.check("bill@shop.example", [IBAN_A])] == ["new"]
    assert [c.status for c in store.check("bill@shop.example", [IBAN_A])] == ["known"]
    assert [c.status for c in store.check("bill@shop.example", [IBAN_B])] == ["changed"]
    assert [c.status for c in store.check("bill@shop.example", [IBAN_B])] == ["changed"]  # every time
    assert store.trust("bill@shop.example", [IBAN_B]) == 1
    assert [c.status for c in store.check("bill@shop.example", [IBAN_B])] == ["known"]
    raw = (tmp_path / "payees.json").read_text()
    assert IBAN_A not in raw and "shop.example" not in raw  # only salted hashes
    assert oct(os.stat(tmp_path / "payees.json").st_mode)[-3:] == "600"


def test_payee_lines_language():
    from personal_agent.mailtriage import PayeeCheck
    [line] = payee_lines([PayeeCheck("DE89 **** 3000", "changed")], "de")
    assert line.startswith("IBAN DE89 **** 3000: GEÄNDERT")


# --- health -------------------------------------------------------------------------------


def test_health_flags_a_failed_login_and_a_stale_check(config):
    svc = service(config, StubWorker(scan=scan_response([], error="auth")))
    view, _ = svc.refresh(True, "console")
    assert any("app password" in n for n in health_notes(view, NOW))
    later = view.accounts[0].checked_at + 7 * 3600
    svc2 = service(config, StubWorker(scan=scan_response([record(1, "x")])), clock=lambda: NOW + 120)
    view2, _ = svc2.refresh(True, "console")
    assert health_notes(view2, NOW + 120) == []
    assert any("not checked" in n for n in health_notes(view2, later))


def test_health_flags_a_quiet_mailbox(config):
    old = NOW - 6 * 86400
    svc = service(config, StubWorker(scan=scan_response([record(1, "x", date=old)])))
    view, _ = svc.refresh(True, "console")
    assert any("working days" in n for n in health_notes(view, NOW))


# --- overview ------------------------------------------------------------------------------


def test_overview_shows_categories_and_labels(config):
    records = [record(1, "Mahnung Rechnung 12"), record(2, "Anfrage Termin"), record(3, "Newsletter", lst=True)]
    svc = service(config, StubWorker(scan=scan_response(records)))
    text, error = svc.command(parse_mail_command("mail refresh"))
    assert not error
    assert "deadline/legal 1" in text and "request 1" in text and "newsletter/automatic 1" in text
    assert "deadline/legal · unread" in text


# --- show + payment check -------------------------------------------------------------------------


def body(text, sender="bill@shop.example"):
    return {"from_name": "Shop", "from_addr": sender, "subject": "Rechnung", "text": text, "signals": []}


def test_show_flags_a_changed_bank_account(config):
    worker = StubWorker(scan=scan_response([record(1, "Rechnung", sender="bill@shop.example")]),
                        body=body(f"IBAN: {IBAN_A}"))
    svc = service(config, worker)
    svc.refresh(True, "console")
    ref = first_ref(svc)
    first, _ = svc.command(parse_mail_command(f"mail show {ref}"))
    assert "Payment check: IBAN DE89 **** 3000: NEW" in first and IBAN_A not in first.split("---")[0]
    worker.body = body(f"Neue Bankverbindung: {IBAN_B}")
    second, _ = svc.command(parse_mail_command(f"mail show {ref}"))
    assert "CHANGED" in second


def test_trust_confirms_the_bank_account(config):
    worker = StubWorker(scan=scan_response([record(1, "Rechnung", sender="bill@shop.example")]),
                        body=body(f"IBAN {IBAN_B}"))
    svc = service(config, worker)
    svc.refresh(True, "console")
    PayeeStore(config.state_dir).check("bill@shop.example", [IBAN_A])
    ref = first_ref(svc)
    text, error = svc.command(parse_mail_command(f"mail trust {ref}"))
    assert not error and "1 bank account" in text
    shown, _ = svc.command(parse_mail_command(f"mail show {ref}"))
    assert "known for this sender" in shown


# --- drafts ---------------------------------------------------------------------------------------


def write_draft(config, ref, text="Hallo Alice,\ndanke für die Anfrage.\n"):
    folder = Path(config.vault_path) / "outputs" / "drafts"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{ref}.md").write_text(text)


def test_draft_sends_the_vault_file_to_the_worker(config):
    worker = StubWorker(scan=scan_response([record(1, "Anfrage Termin")]),
                        draft={"ok": True, "errors": [], "account": "work", "folder": "Drafts"})
    svc = service(config, worker)
    svc.refresh(True, "console")
    ref = first_ref(svc)
    text, error = svc.command(parse_mail_command(f"mail draft {ref}"))
    assert error and "no draft yet" in text and not any(c == "draft" for c, _ in worker.calls)
    write_draft(config, ref)
    text, error = svc.command(parse_mail_command(f"mail draft {ref}"))
    assert not error and "Nothing was sent" in text and "work/Drafts" in text
    command, payload = worker.calls[-1]
    assert command == "draft" and payload["to"] == "alice@example.org"
    assert payload["subject"] == "Re: Anfrage Termin" and "danke" in payload["body"]


def test_draft_refuses_a_symlink_out_of_the_vault(config, tmp_path):
    worker = StubWorker(scan=scan_response([record(1, "Anfrage")]), draft={"ok": True})
    svc = service(config, worker)
    svc.refresh(True, "console")
    ref = first_ref(svc)
    outside = tmp_path / "outside.md"
    outside.write_text("secret")
    folder = Path(config.vault_path) / "outputs" / "drafts"
    folder.mkdir(parents=True)
    (folder / f"{ref}.md").symlink_to(outside)
    text, error = svc.command(parse_mail_command(f"mail draft {ref}"))
    assert error and "regular file" in text
    assert not any(c == "draft" for c, _ in worker.calls)


def test_draft_reports_worker_refusals(config):
    worker = StubWorker(scan=scan_response([record(1, "Anfrage")]),
                        draft={"ok": False, "errors": [{"code": "append_disabled"}]})
    svc = service(config, worker)
    svc.refresh(True, "console")
    ref = first_ref(svc)
    write_draft(config, ref)
    text, error = svc.command(parse_mail_command(f"mail draft {ref}"))
    assert error and "allow_draft_append" in text
    worker.draft_result = WorkerError("timeout")
    text, error = svc.command(parse_mail_command(f"mail draft {ref}"))
    assert error and "timeout" in text


def test_draft_and_trust_need_full_mode_on_telegram(config, monkeypatch):
    monkeypatch.setenv("PA_MAIL_TELEGRAM", "subjects")
    svc = service(Config.from_env(), StubWorker(scan=scan_response([record(1, "x")])))
    for kind in ("draft", "trust"):
        text, error = svc.command(parse_mail_command(f"mail {kind} abcdef"), "telegram")
        assert "PA_MAIL_TELEGRAM=full" in text


@pytest.mark.parametrize("text,kind", [
    ("mail draft a1b2c3", "mail_draft"), ("mail entwurf a1b2c3", "mail_draft"), ("mail trust a1b2c3", "mail_trust"),
    ("mail draft nothex", "mail_usage"), ("!mail draft a1b2c3", None),
])
def test_parse_new_commands(text, kind):
    command = parse_mail_command(text)
    assert (command.kind if command else None) == kind


# --- brief -----------------------------------------------------------------------------------------


def test_brief_mail_summary_from_the_cache_only(config):
    from personal_agent.brief import mail_summary

    assert mail_summary(config, NOW) == ""  # nothing cached yet
    svc = service(config, StubWorker(scan=scan_response([record(1, "Mahnung"), record(2, "Rechnung 3"),
                                                         record(3, "gelesen", seen=True)])))
    svc.refresh(True, "console")
    line = mail_summary(config, NOW)
    assert line.startswith("deadline/legal 1, invoice/payment 1 unread")


def test_brief_mail_summary_names_a_dead_login(config):
    from personal_agent.brief import mail_summary

    service(config, StubWorker(scan=scan_response([], error="auth"))).refresh(True, "console")
    assert "login failed" in mail_summary(config, NOW)
