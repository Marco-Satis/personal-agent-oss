"""Module "focus": the rules in code (limit 3, private, done stays), commands, rights, backup."""

import os
import sqlite3
import stat
import tarfile
import threading

import pytest

from personal_agent.backup import create_backup
from personal_agent.focus import (
    DB_FILE, MAX_FOCUS, MIGRATIONS, SHARED, FocusCommand, FocusError, FocusService, FocusStore, Person,
    parse_focus_command,
)
from personal_agent.roles import Role
from personal_agent.router import Router, RouterConfig

A = Person("anna", Role.OWNER)
B = Person("ben", Role.OWNER)
EMP = Person("emil", Role.EMPLOYEE)
DEV = Person("tablet", Role.DEVICE)
T0 = 1_794_900_000.0  # 17.11.2026, morning


@pytest.fixture
def store(tmp_path):
    clock = {"now": T0}
    s = FocusStore(tmp_path, clock=lambda: clock["now"])
    s.clock_box = clock
    return s


# --- storage ----------------------------------------------------------------------------------


def test_database_is_private_and_versioned(store):
    assert store.schema_version() == len(MIGRATIONS)
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600


def test_newer_schema_is_refused(store):
    store.schema_version()
    conn = sqlite3.connect(store.path)
    conn.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
    conn.close()
    with pytest.raises(FocusError, match="newer"):
        store.items(A, "focus")


def test_symlinked_database_is_refused(tmp_path):
    (tmp_path / "elsewhere.db").write_bytes(b"")
    os.symlink(tmp_path / "elsewhere.db", tmp_path / DB_FILE)
    with pytest.raises(FocusError, match="unsafe"):
        FocusStore(tmp_path).schema_version()


# --- rule 1: at most three open focus items per list ----------------------------------------------


def test_fourth_focus_item_goes_to_later(store):
    for n in range(MAX_FOCUS):
        _, landed = store.add(A, f"task {n}", "focus")
        assert landed == "focus"
    item, landed = store.add(A, "one too many", "focus")
    assert landed == "later" and item.list == "later"
    assert len(store.items(A, "focus", owner="anna")) == MAX_FOCUS
    # each list counts on its own: the shared list and Ben's list are still empty
    assert store.add(A, "shared one", "focus", owner=SHARED)[1] == "focus"
    assert store.add(B, "ben one", "focus")[1] == "focus"


def test_done_item_frees_a_slot_and_stays_visible(store):
    ids = [store.add(A, f"t{n}", "focus")[0].id for n in range(MAX_FOCUS)]
    store.complete(A, ids[0])
    assert store.add(A, "new", "focus")[1] == "focus"
    shown = store.items(A, "focus", owner="anna", include_done_since=T0 - 1)
    assert [i.status for i in shown] == ["open", "open", "open", "done"]  # rule 3: done stays
    with pytest.raises(FocusError, match="already done"):
        store.complete(A, ids[0])


def test_move_into_full_focus_list_is_refused(store):
    for n in range(MAX_FOCUS):
        store.add(A, f"t{n}", "focus")
    item, _ = store.add(A, "idea", "inbox")
    with pytest.raises(FocusError, match="full"):
        store.move(A, item.id, "focus")
    assert store.move(A, item.id, "later").list == "later"


def test_concurrent_adds_never_exceed_the_limit(tmp_path):
    errors = []

    def worker(n):
        try:
            FocusStore(tmp_path).add(A, f"t{n}", "focus")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    s = FocusStore(tmp_path)
    assert len(s.items(A, "focus", owner="anna")) == MAX_FOCUS
    assert len(s.items(A, "later")) == 8 - MAX_FOCUS


# --- rule 2: private items belong to their owner -----------------------------------------------------


def test_private_item_is_invisible_to_the_other_owner(store):
    secret, _ = store.add(A, "surprise for ben", "shopping", private=True)
    assert secret.owner == "anna" and secret.visibility == "private"
    assert [i.text for i in store.items(A, "shopping")] == ["surprise for ben"]
    assert store.items(B, "shopping") == []
    for action in (store.complete, store.drop):
        with pytest.raises(FocusError, match=f"no item #{secret.id}"):  # same answer as a missing id
            action(B, secret.id)
    with pytest.raises(FocusError, match=f"no item #{secret.id}"):
        store.move(B, secret.id, "focus")


def test_private_item_cannot_be_moved_to_someone_else(store):
    item, _ = store.add(A, "mine", "inbox", private=True)
    with pytest.raises(FocusError, match="owner"):
        store.move(A, item.id, "focus", owner=SHARED)
    with pytest.raises(FocusError, match="cannot be private"):
        store.add(A, "x", "focus", owner=SHARED, private=True)


def test_ids_are_not_guessable_into_other_lists(store):
    # IDOR: a known id does not give access beyond the rights of the asker
    item, _ = store.add(A, "anna's focus", "focus")
    with pytest.raises(FocusError, match="role"):
        store.complete(EMP, item.id)  # employees edit only their own and shared lists
    with pytest.raises(FocusError, match="role"):
        store.complete(DEV, item.id)
    assert store.complete(B, item.id).status == "done"  # the other owner may (focus.edit_others)


# --- rights from roles.py ----------------------------------------------------------------------------


def test_device_may_only_add_to_the_inbox(store):
    assert store.add(DEV, "call the supplier", "inbox")[0].owner == "tablet"
    assert store.add(DEV, "for everybody", "inbox", owner=SHARED)[0].owner == SHARED
    for name in ("focus", "later", "shopping", "joy"):
        with pytest.raises(FocusError, match="role"):
            store.add(DEV, "x", name)


def test_employee_edits_own_and_shared_but_not_others(store):
    assert store.add(EMP, "mine", "focus")[1] == "focus"
    assert store.add(EMP, "ours", "focus", owner=SHARED)[1] == "focus"
    with pytest.raises(FocusError, match="role"):
        store.add(EMP, "for anna", "focus", owner="anna")
    assert store.add(A, "for emil", "focus", owner="emil")[1] == "focus"  # owner: focus.edit_others


def test_unknown_role_sees_and_changes_nothing(store):
    store.add(A, "visible", "focus")
    ghost = Person("ghost", "admin")
    assert store.items(ghost, "focus") == []
    with pytest.raises(FocusError, match="role"):
        store.add(ghost, "x", "inbox")


def test_text_is_cleaned_and_capped(store):
    item, _ = store.add(A, "  buy​  milk \n", "shopping")
    assert item.text == "buy milk" and item.owner == SHARED
    with pytest.raises(FocusError, match="empty"):
        store.add(A, " ​ ", "inbox")
    with pytest.raises(FocusError, match="longer"):
        store.add(A, "x" * 301, "inbox")


# --- commands ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("fokus", FocusCommand("show", "focus")),
    ("Focus", FocusCommand("show", "focus")),
    ("fokus Angebot Müller", FocusCommand("add", "focus", "Angebot Müller")),
    ("fokus gemeinsam Steuer", FocusCommand("add", "focus", "Steuer", shared=True)),
    ("einkauf privat Geschenk", FocusCommand("add", "shopping", "Geschenk", private=True)),
    ("fokus #12", FocusCommand("move", "focus", item_id=12)),
    ("fokus #12 gemeinsam", FocusCommand("move", "focus", shared=True, item_id=12)),
    ("erledigt 7", FocusCommand("done", item_id=7)),
    ("done #7", FocusCommand("done", item_id=7)),
    ("verwerfen 3", FocusCommand("drop", item_id=3)),
    ("erledigt", FocusCommand("usage")),
    ("erledigt alles", FocusCommand("usage")),
    ("fokus gemeinsam privat x", FocusCommand("usage")),
    ("später", FocusCommand("show", "later")),
    ("freude Kaffee in der Sonne", FocusCommand("add", "joy", "Kaffee in der Sonne")),
    ("fokus gemeinsam", FocusCommand("add", "focus", "gemeinsam")),  # a lone keyword is text
])
def test_parse(text, expected):
    assert parse_focus_command(text) == expected


@pytest.mark.parametrize("text", ["", "!fokus x", "focusing on it", "was ist fokus", "mail", "task x"])
def test_not_a_focus_command(text):
    assert parse_focus_command(text) is None


def test_service_round_trip(store):
    svc = FocusService(store, A, "de")
    assert svc.command(parse_focus_command("fokus Angebot schreiben")) == ("#1 in Fokus eingetragen", False)
    svc.command(parse_focus_command("fokus gemeinsam Steuer"))
    board, err = svc.command(parse_focus_command("fokus"))
    assert not err and "Fokus · du" in board and "#1 Angebot schreiben" in board and "Fokus · gemeinsam" in board
    assert svc.command(parse_focus_command("erledigt 1"))[0] == "#1 erledigt: Angebot schreiben"
    assert "✓ #1 Angebot schreiben" in svc.command(parse_focus_command("fokus"))[0]  # done stays today
    text, err = svc.command(parse_focus_command("erledigt 99"))
    assert err and text == "no item #99"
    assert svc.command(parse_focus_command("einkauf"))[0] == "Einkaufsliste\n  (leer)"


def test_done_items_from_yesterday_leave_the_board(store):
    svc = FocusService(store, A, "en")
    svc.command(parse_focus_command("focus old"))
    svc.command(parse_focus_command("done 1"))
    store.clock_box["now"] = T0 + 86_400
    assert "old" not in svc.command(parse_focus_command("focus"))[0]


def test_full_list_reply(store):
    svc = FocusService(store, A, "en")
    for n in range(MAX_FOCUS):
        svc.command(parse_focus_command(f"focus t{n}"))
    assert svc.command(parse_focus_command("focus t3")) == ("focus list full (3): #4 went to Later", False)


class NoSession:
    def send(self, *_a, **_k):
        raise AssertionError("focus commands need no model")

    def close(self, timeout_s=10):
        pass


class RefusingGuard:
    def check(self, *_a, **_k):
        raise AssertionError("focus commands need no quota")


def test_router_answers_focus_at_tier_1(store):
    router = Router(NoSession(), None, RefusingGuard(), RouterConfig(), statuses=lambda: [],
                    focus=FocusService(store, A, "en"))
    assert router.is_tier1("fokus Angebot")
    reply = router.handle("fokus Angebot")
    assert reply.tier == 1 and reply.text == "added #1 to Focus"
    assert "Focus lists" in router.handle("help").text


def test_router_without_focus_module_leaves_the_words_alone():
    router = Router(NoSession(), None, RefusingGuard(), RouterConfig(small_model=None), statuses=lambda: [])
    assert not router.is_tier1("fokus Angebot")


# --- backup -----------------------------------------------------------------------------------------------


def test_backup_holds_a_consistent_copy_of_the_database(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    s = FocusStore(state)
    s.add(A, "kept", "focus")
    (state / "focus.db-journal").write_bytes(b"stale journal")
    result = create_backup(state, tmp_path / "vault")
    out = tmp_path / "out"
    with tarfile.open(result.path) as tar:
        names = tar.getnames()
        assert "state/focus.db" in names and "state/focus.db-journal" not in names
        tar.extract("state/focus.db", out, filter="data")
    restored = FocusStore(out / "state")
    assert [i.text for i in restored.items(A, "focus")] == ["kept"]


def test_backup_fails_loudly_on_a_broken_database(tmp_path):
    from personal_agent.backup import BackupError, list_backups, backup_dir
    state = tmp_path / "state"
    state.mkdir()
    (state / DB_FILE).write_bytes(b"not a database at all, but long enough to have a header" * 4)
    with pytest.raises(BackupError, match="focus.db"):
        create_backup(state, tmp_path / "vault")
    assert list_backups(backup_dir(state)) == []  # no half archive left behind


# --- F2: evening review, brief and evening views ---------------------------------------------------------


def test_review_highlights_and_good_day(store):
    svc = FocusService(store, A, "de")
    svc.command(parse_focus_command("fokus Angebot"))
    svc.command(parse_focus_command("erledigt 1"))
    assert svc.command(parse_focus_command("rückblick Kunde war begeistert")) == ("Highlight gespeichert (1/2)", False)
    assert svc.command(parse_focus_command("rückblick Sonne"))[0] == "Highlight gespeichert (2/2)"
    text, err = svc.command(parse_focus_command("rückblick noch eins"))
    assert err and "2 highlights" in text
    assert svc.command(parse_focus_command("guter tag sport paar")) == ("Guter Tag: Zeit als Paar, Sport", False)
    assert svc.command(parse_focus_command("guter tag fliegen"))[0].startswith("So geht es: guter tag")
    shown = svc.command(parse_focus_command("rückblick"))[0]
    assert "✓ #1 Angebot" in shown and "★ Kunde war begeistert" in shown and "Guter Tag: Zeit als Paar, Sport" in shown
    # reviews are per person: the other owner has their own
    assert store.review(B) == ([], [])
    assert store.schema_version() == 2


def test_device_cannot_write_a_review(store):
    with pytest.raises(FocusError, match="role"):
        store.add_highlight(DEV, "x")


def test_brief_and_evening_views(store):
    svc = FocusService(store, A, "de")
    for text in ("fokus Angebot", "fokus gemeinsam Steuer", "inbox Idee", "einkauf Milch", "freude Kaffee draußen"):
        svc.command(parse_focus_command(text))
    counts, lines = svc.brief_view()
    assert counts == "dein Fokus 1/3 offen · gemeinsam 1 · Inbox 1 · Einkauf 1"
    assert lines == ["Fokus · du: #1 Angebot", "Fokus · gemeinsam: #2 Steuer", "Freudeinsel: Kaffee draußen"]
    svc.command(parse_focus_command("erledigt 2"))
    evening = svc.evening_lines()
    assert evening[0] == "Heute erledigt: 1: Steuer" and evening[1] == "Noch offen in deinem Fokus: #1 Angebot"
    assert evening[2].startswith("Rückblick:")


def test_brief_and_evening_read_the_lists_without_creating_them(tmp_path):
    from datetime import date

    from personal_agent.brief import BriefData, focus_summary, render_markdown, render_plain
    from personal_agent.config import Config
    from personal_agent.focus import OWNER_ID
    from personal_agent.rhythm import focus_evening_lines

    config = Config(state_dir=tmp_path, status_dir=tmp_path / "s", language="de")
    assert focus_summary(config, T0) == ("", ()) and focus_evening_lines(config, T0) == []
    assert not (tmp_path / DB_FILE).exists()  # nothing created just for reading
    store = FocusStore(tmp_path, clock=lambda: T0)
    me = Person(OWNER_ID)
    store.add(me, "Angebot <b>", "focus")
    store.add(me, "Steuer", "focus", owner=SHARED)
    counts, lines = focus_summary(config, T0)
    assert counts.startswith("dein Fokus 1/3 offen") and lines[0] == "Fokus · du: #1 Angebot <b>"
    data = BriefData(day=date(2026, 11, 17), weekday="Dienstag", now_ts=T0, has_vault=False, focus="", tasks=(),
                     task_total=0, status_counts=(), statuses=(), quota="q", captures=0, since_text="07:30",
                     lists=counts, list_lines=lines)
    md = render_markdown(data, "de")
    assert "## Listen" in md and "- Fokus · du: #1 Angebot <b>" in md
    assert "Listen: dein Fokus 1/3 offen" in render_plain(data, "de", "counts", "outputs/x.md")
    assert "Angebot" not in render_plain(data, "de", "counts", "outputs/x.md")  # counts mode: no item text
    assert "Angebot" in render_plain(data, "de", "full", "outputs/x.md")
    store.complete(me, 1)
    assert focus_evening_lines(config, T0 + 60)[0] == "Heute erledigt: 1: Angebot <b>"
    off = Config(state_dir=tmp_path, status_dir=tmp_path / "s", modules=("brief",))
    assert focus_summary(off, T0) == ("", ()) and focus_evening_lines(off, T0) == []
