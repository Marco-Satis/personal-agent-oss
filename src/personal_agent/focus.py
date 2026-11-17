"""Focus lists, inbox, shopping list and joy island (module "focus").

The structure behind the day: every person has a focus list with at most three open items,
there is a shared list, an inbox for everything that comes to mind, a "later" list, a
shopping list and a joy island. The rules live here, in code, not in a prompt:

1. At most MAX_FOCUS open focus items per list (person or "shared"); a fourth goes to "later".
2. A private item is visible and editable for its owner only, whatever their role.
3. Done items stay (status "done" with a time), so the evening review can show them;
   "drop" marks an item as dropped, nothing is ever deleted here.
4. Nothing moves between lists unless a person says so (the assistant may only suggest).

Storage is one SQLite file (stdlib, state_dir/focus.db, 0600), schema versioned with
PRAGMA user_version and migrations that only go forward. Every call opens its own short
connection, so the chat, Telegram, ntfy and a later web view can use it at the same time.
Rights come from roles.py; the caller (router) passes a trusted Person, never text.
"""

from __future__ import annotations

import os
import re
import sqlite3
import stat
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .roles import Role, allowed
from .textsafe import strip_invisible

DB_FILE = "focus.db"
MAX_FOCUS = 3
MAX_TEXT = 300
SHARED = "shared"
OWNER_ID = "me"  # the single person of an installation until channels are paired per person (M4)
LISTS = ("focus", "inbox", "later", "shopping", "joy")
SHARED_LISTS = ("shopping", "joy")  # one list for everybody, no personal owner
SHOW_LIMIT = 30
LOCK_TIMEOUT_S = 15  # a busy writer elsewhere (chat, Telegram, ntfy) is waited for

MIGRATIONS = (
    """
    CREATE TABLE items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT NOT NULL,
        list TEXT NOT NULL CHECK (list IN ('focus', 'inbox', 'later', 'shopping', 'joy')),
        owner TEXT NOT NULL,
        visibility TEXT NOT NULL CHECK (visibility IN ('team', 'private')),
        status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'dropped')),
        horizon TEXT CHECK (horizon IN ('day', 'week', 'month')),
        day TEXT,
        goal_id INTEGER,
        urgent INTEGER,
        important INTEGER,
        source TEXT,
        created_by TEXT NOT NULL,
        created_at REAL NOT NULL,
        done_at REAL,
        suggestion TEXT
    );
    CREATE INDEX items_owner ON items (owner, list, status);
    """,
)

_SPACES = re.compile(r"\s+")


class FocusError(RuntimeError):
    """A rule or a right says no; the message is safe to show."""


@dataclass(frozen=True)
class Person:
    """Who is asking. Built by trusted code (channel pairing), never from message text."""
    id: str
    role: Role | str = Role.OWNER


@dataclass(frozen=True)
class Item:
    id: int
    text: str
    list: str
    owner: str
    visibility: str
    status: str
    day: str | None
    created_by: str
    created_at: float
    done_at: float | None


def clean(text: str) -> str:
    text = _SPACES.sub(" ", strip_invisible(text or "")).strip()
    if not text:
        raise FocusError("empty text")
    if len(text) > MAX_TEXT:
        raise FocusError(f"text longer than {MAX_TEXT} characters")
    return text


class FocusStore:
    def __init__(self, state_dir: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(state_dir) / DB_FILE
        self.clock = clock

    # --- connection and schema ---------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        parent = self.path.parent
        if not parent.is_dir():
            raise FocusError("the state directory does not exist")
        try:
            info = os.lstat(self.path)
        except FileNotFoundError:
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                os.close(fd)
            except FileExistsError:
                pass  # another connection created it a moment ago
            info = os.lstat(self.path)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
            raise FocusError("unsafe focus database path")
        conn = sqlite3.connect(self.path, timeout=LOCK_TIMEOUT_S, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        self._migrate(conn)
        return conn

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > len(MIGRATIONS):
            conn.close()
            raise FocusError("the focus database is newer than this program")
        if version == len(MIGRATIONS):
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]  # another process may have migrated
            for number in range(version, len(MIGRATIONS)):
                for statement in MIGRATIONS[number].split(";"):
                    if statement.strip():
                        conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {number + 1}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def schema_version(self) -> int:
        conn = self._connect()
        try:
            return conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()

    # --- rights ---------------------------------------------------------------------

    @staticmethod
    def _may_see(person: Person, item: Item) -> bool:
        if item.visibility == "private":
            return item.owner == person.id
        return allowed(person.role, "dashboard.view")

    @staticmethod
    def _check_edit(person: Person, list_name: str, owner: str, visibility: str) -> None:
        if visibility == "private":
            if owner != person.id:
                raise FocusError("private items belong to their owner")
            action = "inbox.add" if list_name == "inbox" else "focus.edit_own"
        elif list_name == "inbox" and owner in (person.id, SHARED):
            action = "inbox.add"
        elif owner in (person.id, SHARED):
            action = "focus.edit_own"
        else:
            action = "focus.edit_others"
        if not allowed(person.role, action):
            raise FocusError("your role may not change this list")

    # --- writes -------------------------------------------------------------------

    def add(self, person: Person, text: str, list_name: str, *, owner: str | None = None,
            private: bool = False, source: str | None = None) -> tuple[Item, str]:
        """Add an item; returns (item, list it landed in). A full focus list sends it to "later"."""
        if list_name not in LISTS:
            raise FocusError("unknown list")
        text = clean(text)
        owner = SHARED if list_name in SHARED_LISTS else (owner or person.id)
        if private and owner == SHARED:
            if list_name in SHARED_LISTS:
                owner = person.id  # a private shopping item (a surprise) belongs to its author
            else:
                raise FocusError("a shared item cannot be private")
        visibility = "private" if private else "team"
        self._check_edit(person, list_name, owner, visibility)
        now = self.clock()
        day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")  # the count and the insert must not interleave
            landed = list_name
            if list_name == "focus" and self._open_focus(conn, owner) >= MAX_FOCUS:
                landed = "later"
            cur = conn.execute(
                "INSERT INTO items (text, list, owner, visibility, day, source, created_by, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (text, landed, owner, visibility, day, source, person.id, now))
            conn.execute("COMMIT")
            item = self._get(conn, cur.lastrowid)
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return item, landed

    @staticmethod
    def _open_focus(conn: sqlite3.Connection, owner: str) -> int:
        return conn.execute(
            "SELECT COUNT(*) FROM items WHERE list = 'focus' AND owner = ? AND status = 'open'",
            (owner,)).fetchone()[0]

    @staticmethod
    def _get(conn: sqlite3.Connection, item_id: int) -> Item:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise FocusError(f"no item #{item_id}")
        return _item(row)

    def _change(self, person: Person, item_id: int, apply: Callable[[sqlite3.Connection, Item], None]) -> Item:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
            item = _item(row) if row is not None else None
            if item is None or not self._may_see(person, item):
                raise FocusError(f"no item #{item_id}")  # the same answer: private items stay invisible
            self._check_edit(person, item.list, item.owner, item.visibility)
            apply(conn, item)
            conn.execute("COMMIT")
            return self._get(conn, item_id)
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def complete(self, person: Person, item_id: int) -> Item:
        def apply(conn: sqlite3.Connection, item: Item) -> None:
            if item.status != "open":
                raise FocusError(f"#{item_id} is already {item.status}")
            conn.execute("UPDATE items SET status = 'done', done_at = ? WHERE id = ?", (self.clock(), item_id))
        return self._change(person, item_id, apply)

    def drop(self, person: Person, item_id: int) -> Item:
        def apply(conn: sqlite3.Connection, item: Item) -> None:
            if item.status != "open":
                raise FocusError(f"#{item_id} is already {item.status}")
            conn.execute("UPDATE items SET status = 'dropped', done_at = ? WHERE id = ?", (self.clock(), item_id))
        return self._change(person, item_id, apply)

    def move(self, person: Person, item_id: int, list_name: str, owner: str | None = None) -> Item:
        """Move an open item to another list (and owner). Into a full focus list: refused."""
        if list_name not in LISTS:
            raise FocusError("unknown list")

        def apply(conn: sqlite3.Connection, item: Item) -> None:
            if item.status != "open":
                raise FocusError(f"#{item_id} is already {item.status}")
            if owner is not None:
                new_owner = owner
            elif list_name in SHARED_LISTS and item.visibility == "team":
                new_owner = SHARED
            elif item.list in SHARED_LISTS and item.owner == SHARED:
                new_owner = person.id  # from the shopping list or joy island to a personal list
            else:
                new_owner = item.owner
            if item.visibility == "private" and new_owner != item.owner:
                raise FocusError("private items belong to their owner")
            self._check_edit(person, list_name, new_owner, item.visibility)
            if list_name == "focus" and (item.list != "focus" or new_owner != item.owner) \
                    and self._open_focus(conn, new_owner) >= MAX_FOCUS:
                raise FocusError(f"focus list is full ({MAX_FOCUS}); finish or move one first")
            conn.execute("UPDATE items SET list = ?, owner = ? WHERE id = ?", (list_name, new_owner, item_id))
        return self._change(person, item_id, apply)

    # --- reads --------------------------------------------------------------------

    def items(self, person: Person, list_name: str, *, owner: str | None = None,
              include_done_since: float | None = None, limit: int = SHOW_LIMIT) -> list[Item]:
        """Visible items of one list, open first (oldest first), then done since a time."""
        if list_name not in LISTS:
            raise FocusError("unknown list")
        if not allowed(person.role, "dashboard.view"):
            return []
        sql = "SELECT * FROM items WHERE list = ? AND (status = 'open'"
        args: list[object] = [list_name]
        if include_done_since is not None:
            sql += " OR (status = 'done' AND done_at >= ?)"
            args.append(include_done_since)
        sql += ") AND (visibility = 'team' OR owner = ?)"
        args.append(person.id)
        if owner is not None:
            sql += " AND owner = ?"
            args.append(owner)
        sql += " ORDER BY status = 'done', id LIMIT ?"
        args.append(max(1, limit))
        conn = self._connect()
        try:
            return [_item(row) for row in conn.execute(sql, args)]
        finally:
            conn.close()

    def owner_of(self, person: Person, item_id: int) -> str | None:
        """The owner of an item the person may see, or None (missing or private to someone else)."""
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        item = _item(row)
        return item.owner if self._may_see(person, item) else None

    def focus_owners(self, person: Person) -> list[str]:
        """Owners with visible open or recent focus items: the person first, then shared, then others."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT DISTINCT owner FROM items WHERE list = 'focus' AND (visibility = 'team' OR owner = ?)",
                (person.id,)).fetchall()
        finally:
            conn.close()
        others = sorted(r[0] for r in rows if r[0] not in (person.id, SHARED))
        return [person.id, SHARED] + others


def _item(row: sqlite3.Row) -> Item:
    return Item(row["id"], row["text"], row["list"], row["owner"], row["visibility"], row["status"],
                row["day"], row["created_by"], row["created_at"], row["done_at"])


# --- tier-1 commands ---------------------------------------------------------------

@dataclass(frozen=True)
class FocusCommand:
    kind: str           # show, add, done, drop, move, usage
    list: str = ""
    arg: str = ""
    shared: bool = False
    private: bool = False
    item_id: int = 0


LIST_WORDS = {
    "focus": ("fokus", "focus"),
    "inbox": ("inbox", "eingang"),
    "later": ("später", "spaeter", "later"),
    "shopping": ("einkauf", "einkaufsliste", "shopping"),
    "joy": ("freude", "freudeinsel", "joy"),
}
DONE_WORDS = ("erledigt", "done")
DROP_WORDS = ("verwerfen", "drop")
SHARED_WORDS = ("gemeinsam", "shared")
PRIVATE_WORDS = ("privat", "private")
_ID = re.compile(r"#?(\d{1,9})")
_WORD_TO_LIST = {word: name for name, words in LIST_WORDS.items() for word in words}


def parse_focus_command(text: str) -> FocusCommand | None:
    """The tier-1 focus command in text, or None. "!fokus ..." always goes to the assistant."""
    s = (text or "").strip()
    if not s or s.startswith("!"):
        return None
    first, _, rest = s.partition(" ")
    word = first.lower()
    rest = rest.strip()
    if word in DONE_WORDS or word in DROP_WORDS:
        match = _ID.fullmatch(rest)
        if not match:
            return FocusCommand("usage")
        return FocusCommand("done" if word in DONE_WORDS else "drop", item_id=int(match.group(1)))
    list_name = _WORD_TO_LIST.get(word)
    if list_name is None:
        return None
    if not rest:
        return FocusCommand("show", list_name)
    move = re.fullmatch(r"#(\d{1,9})(?:\s+(gemeinsam|shared))?", rest, re.I)
    if move:
        return FocusCommand("move", list_name, shared=bool(move.group(2)), item_id=int(move.group(1)))
    shared = private = False
    while True:
        head, _, tail = rest.partition(" ")
        if head.lower() in SHARED_WORDS and tail.strip():
            shared, rest = True, tail.strip()
        elif head.lower() in PRIVATE_WORDS and tail.strip():
            private, rest = True, tail.strip()
        else:
            break
    if shared and private:
        return FocusCommand("usage")
    return FocusCommand("add", list_name, rest, shared=shared, private=private)


TOOL_KEYS = frozenset({"op", "list", "text", "item_id", "shared", "private"})
AGENT_SOURCE = "agent"


def tool_command(tool_input: object) -> tuple[FocusCommand | None, str | None]:
    """(command, problem) for an input of the assistant's focus tool. Checked again on every call."""
    if not isinstance(tool_input, dict) or not set(tool_input) <= TOOL_KEYS:
        return None, "invalid focus request"
    op = tool_input.get("op")
    list_name = tool_input.get("list", "")
    text = tool_input.get("text", "")
    item_id = tool_input.get("item_id", 0)
    shared = tool_input.get("shared", False)
    private = tool_input.get("private", False)
    if not isinstance(shared, bool) or not isinstance(private, bool) or not isinstance(text, str) \
            or not isinstance(list_name, str) or isinstance(item_id, bool) or not isinstance(item_id, int):
        return None, "invalid focus request"
    if list_name and list_name not in LISTS:
        return None, "unknown list"
    if op == "show":
        return FocusCommand("show", list_name or "focus"), None
    if op == "add":
        if not list_name or not text.strip() or len(text) > MAX_TEXT or (shared and private):
            return None, f"add needs list and 1 to {MAX_TEXT} characters of text, not shared and private at once"
        return FocusCommand("add", list_name, text, shared=shared, private=private), None
    if op in ("move", "done", "drop"):
        if item_id < 1:
            return None, f"{op} needs item_id"
        if op == "move" and not list_name:
            return None, "move needs list"
        return FocusCommand(op, list_name if op == "move" else "", shared=shared, item_id=item_id), None
    return None, "unknown op"


TEXTS = {
    "en": {
        "focus": "Focus", "inbox": "Inbox", "later": "Later", "shopping": "Shopping list", "joy": "Joy island",
        "you": "you", "shared": "shared", "empty": "(empty)", "added": "added #{id} to {list}",
        "full": "focus list full ({max}): #{id} went to Later",
        "done": "#{id} done: {text}", "dropped": "#{id} dropped: {text}", "moved": "#{id} moved to {list}",
        "usage": "usage: fokus [gemeinsam|privat] <text> · fokus #<n> [gemeinsam] · erledigt <n> · "
                 "verwerfen <n> · inbox|später|einkauf|freude [privat] [<text>]",
    },
    "de": {
        "focus": "Fokus", "inbox": "Inbox", "later": "Später", "shopping": "Einkaufsliste", "joy": "Freudeinsel",
        "you": "du", "shared": "gemeinsam", "empty": "(leer)", "added": "#{id} in {list} eingetragen",
        "full": "Fokusliste voll ({max}): #{id} liegt unter Später",
        "done": "#{id} erledigt: {text}", "dropped": "#{id} verworfen: {text}", "moved": "#{id} nach {list} verschoben",
        "usage": "So geht es: fokus [gemeinsam|privat] <Text> · fokus #<n> [gemeinsam] · erledigt <n> · "
                 "verwerfen <n> · inbox|später|einkauf|freude [privat] [<Text>]",
    },
}


class FocusService:
    """Tier-1 focus commands for one trusted person; no model, no quota."""

    def __init__(self, store: FocusStore, person: Person, language: str = "en") -> None:
        self.store = store
        self.person = person
        self.t = TEXTS.get(language, TEXTS["en"])

    def help_line(self) -> str:
        return ("Focus lists (at most 3 open per person and shared), inbox, later, shopping list, joy island: "
                + self.t["usage"])

    def own_change(self, cmd: FocusCommand) -> bool:
        """Whether the command only reads or touches the person's own or the shared lists.

        Those changes the assistant may make without asking (Marco 17.11.2026); a change to
        another person's list needs the person's approval. Unknown or invisible items count
        as not own, so the asking path gives the usual "no item" answer.
        """
        if cmd.kind in ("show", "usage"):
            return True
        if cmd.kind == "add":
            return True  # add never names another owner
        owner = self.store.owner_of(self.person, cmd.item_id)
        return owner in (self.person.id, SHARED)

    def command(self, cmd: FocusCommand, source: str | None = None) -> tuple[str, bool]:
        try:
            return self._run(cmd, source), False
        except FocusError as exc:
            return str(exc), True
        except (OSError, sqlite3.Error) as exc:
            return f"focus unavailable: {type(exc).__name__}", True

    def _run(self, cmd: FocusCommand, source: str | None = None) -> str:
        t, store, person = self.t, self.store, self.person
        if cmd.kind == "usage":
            return t["usage"]
        if cmd.kind == "done":
            item = store.complete(person, cmd.item_id)
            return t["done"].format(id=item.id, text=item.text)
        if cmd.kind == "drop":
            item = store.drop(person, cmd.item_id)
            return t["dropped"].format(id=item.id, text=item.text)
        if cmd.kind == "move":
            item = store.move(person, cmd.item_id, cmd.list, SHARED if cmd.shared else None)
            return t["moved"].format(id=item.id, list=t[item.list])
        if cmd.kind == "add":
            owner = SHARED if cmd.shared else None
            item, landed = store.add(person, cmd.arg, cmd.list, owner=owner, private=cmd.private, source=source)
            if landed != cmd.list:
                return t["full"].format(max=MAX_FOCUS, id=item.id)
            return t["added"].format(id=item.id, list=t[landed])
        if cmd.list == "focus":
            return self._board()
        return self._render(t[cmd.list], store.items(person, cmd.list))

    def _owner_label(self, owner: str) -> str:
        if owner == self.person.id:
            return self.t["you"]
        if owner == SHARED:
            return self.t["shared"]
        return owner

    def _board(self) -> str:
        midnight = datetime.fromtimestamp(self.store.clock()).replace(hour=0, minute=0, second=0, microsecond=0)
        since = midnight.timestamp()
        blocks = []
        for owner in self.store.focus_owners(self.person):
            items = self.store.items(self.person, "focus", owner=owner, include_done_since=since)
            blocks.append(self._render(f"{self.t['focus']} · {self._owner_label(owner)}", items))
        return "\n\n".join(blocks)

    def _render(self, title: str, items: list[Item]) -> str:
        lines = [title]
        if not items:
            lines.append(f"  {self.t['empty']}")
        for item in items:
            mark = "✓" if item.status == "done" else "·"
            lock = " 🔒" if item.visibility == "private" else ""
            lines.append(f"  {mark} #{item.id} {item.text}{lock}")
        return "\n".join(lines)


__all__ = [
    "DB_FILE", "MAX_FOCUS", "OWNER_ID", "SHARED", "FocusCommand", "FocusError", "FocusService", "FocusStore", "Item",
    "Person", "parse_focus_command", "tool_command",
]
