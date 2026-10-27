"""A small IMAP4rev1 server over TLS on 127.0.0.1, for tests only.

It supports what the read-only mail worker uses (CAPABILITY, LOGIN, STATUS, EXAMINE,
UID SEARCH, UID FETCH with UID/FLAGS/INTERNALDATE/RFC822.SIZE, BODY.PEEK[HEADER.FIELDS
(...)] and BODY.PEEK[]<0.n>, NOOP, LOGOUT). Every command is recorded, with the LOGIN
password replaced by ***. Anything that could change the mailbox (SELECT, STORE, COPY,
MOVE, EXPUNGE, CREATE, DELETE, APPEND, a non-PEEK BODY[, RFC822, RFC822.TEXT, ...) is
recorded in `violations` and answered NO; flags never change.

Modes (a set of strings) simulate hostile or broken servers: logindisabled, slow
(`delay` seconds before each response), giant_literal, malformed, unsolicited (an
interleaved FETCH for an unrequested UID with another sender), reorder (literal items
first), split (one message's items in two responses), conflict (two responses for one
UID that disagree), no_imap4rev1, drafts (LIST and APPEND are served: appended messages
land in `appended` and in their folder; without this mode both are violations).
"""

from __future__ import annotations

import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
FORBIDDEN_COMMANDS = ("SELECT", "STORE", "COPY", "MOVE", "EXPUNGE", "CREATE", "DELETE", "RENAME", "SUBSCRIBE",
                      "UNSUBSCRIBE", "APPEND", "CLOSE", "STARTTLS", "AUTHENTICATE", "FETCH", "SEARCH", "LIST")


@dataclass
class Msg:
    uid: int
    raw: bytes
    flags: set = field(default_factory=set)
    internaldate: str = "19-Oct-2026 08:12:00 +0200"


@dataclass
class Folder:
    wire: str              # the mailbox name as sent on the wire (modified UTF-7)
    uidvalidity: int = 1700000000
    messages: list = field(default_factory=list)

    @property
    def uidnext(self) -> int:
        return max((m.uid for m in self.messages), default=0) + 1


def _unquote(token: str) -> str:
    if token.startswith('"') and token.endswith('"') and len(token) >= 2:
        return re.sub(r"\\(.)", r"\1", token[1:-1])
    return token


def _split_args(rest: str) -> list[str]:
    """Top-level arguments: quoted strings, atoms and parenthesized groups (kept raw)."""
    out: list[str] = []
    i, n = 0, len(rest)
    while i < n:
        c = rest[i]
        if c == " ":
            i += 1
            continue
        if c == '"':
            j = i + 1
            while j < n and rest[j] != '"':
                j += 2 if rest[j] == "\\" else 1
            out.append(rest[i:j + 1])
            i = j + 1
            continue
        if c == "(":
            depth, j = 0, i
            while j < n:
                if rest[j] == "(":
                    depth += 1
                elif rest[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            out.append(rest[i:j + 1])
            i = j + 1
            continue
        j = i
        while j < n and rest[j] != " ":
            if rest[j] == "[":
                j = rest.index("]", j)
            j += 1
        out.append(rest[i:j])
        i = j
    return out


def _uid_set(text: str, uids: list[int]) -> list[int]:
    top = max(uids, default=0)
    chosen: set[int] = set()
    for part in text.split(","):
        if ":" in part:
            a, b = part.split(":", 1)
            low = top if a == "*" else int(a)
            high = top if b == "*" else int(b)
            low, high = min(low, high), max(low, high)
            chosen.update(u for u in uids if low <= u <= high)
        else:
            value = top if part == "*" else int(part)
            if value in uids:
                chosen.add(value)
    return sorted(chosen)


def header_fields(raw: bytes, names: list[str]) -> bytes:
    wanted = {n.lower() for n in names}
    head = raw.split(b"\r\n\r\n", 1)[0] if b"\r\n\r\n" in raw else raw.split(b"\n\n", 1)[0]
    out: list[bytes] = []
    keep = False
    for line in head.replace(b"\r\n", b"\n").split(b"\n"):
        if line[:1] in (b" ", b"\t"):
            if keep:
                out.append(line)
            continue
        name = line.split(b":", 1)[0].decode("ascii", "replace").strip().lower()
        keep = name in wanted
        if keep:
            out.append(line)
    return b"\r\n".join(out) + b"\r\n\r\n"


class FakeIMAP:
    def __init__(self, certfile: str | None, keyfile: str | None, *, user: str = "me@example.org",
                 password: str = "pw", folders: dict[str, Folder] | None = None, modes: set[str] | None = None,
                 delay: float = 0.0) -> None:
        self.user = user
        self.password = password
        self.folders = folders if folders is not None else {"INBOX": Folder("INBOX")}
        self.modes = set(modes or ())
        self.delay = delay
        self.commands: list[str] = []
        self.violations: list[str] = []
        self.appended: list[tuple[str, str, bytes]] = []  # (folder, flags, raw)
        self.logins = 0
        self.context: ssl.SSLContext | None = None
        if certfile:
            self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.context.load_cert_chain(certfile, keyfile)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self._closed = threading.Event()
        self._conns: list[socket.socket] = []
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def __enter__(self) -> "FakeIMAP":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._closed.set()
        try:
            self.sock.close()
        except OSError:
            pass
        for conn in list(self._conns):
            try:
                conn.close()
            except OSError:
                pass

    def _accept(self) -> None:
        while not self._closed.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self._conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    # --- one connection ---

    def _serve(self, raw: socket.socket) -> None:
        conn = raw
        try:
            raw.settimeout(30)
            if self.context is not None:
                conn = self.context.wrap_socket(raw, server_side=True)
                self._conns.append(conn)
            reader = conn.makefile("rb")
            state = {"auth": False, "selected": None, "reader": reader}
            self._send(conn, b"* OK fake IMAP ready\r\n")
            while not self._closed.is_set():
                line = reader.readline(100_000)
                if not line:
                    return
                text = line.rstrip(b"\r\n").decode("utf-8", "replace")
                match = re.match(r"(\S+) (\S+)(?: (.*))?$", text)
                if not match:
                    self._send(conn, b"* BAD what\r\n")
                    continue
                tag, command, rest = match.group(1), match.group(2).upper(), match.group(3) or ""
                if not self._handle(conn, tag, command, rest, state):
                    return
        except (OSError, ssl.SSLError, ValueError):
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _send(self, conn: socket.socket, data: bytes) -> None:
        if self.delay:
            time.sleep(self.delay)
        conn.sendall(data)

    def _ok(self, conn: socket.socket, tag: str, text: str = "done") -> None:
        self._send(conn, f"{tag} OK {text}\r\n".encode())

    def _no(self, conn: socket.socket, tag: str, text: str = "no") -> None:
        self._send(conn, f"{tag} NO {text}\r\n".encode())

    def _handle(self, conn: socket.socket, tag: str, command: str, rest: str, state: dict) -> bool:
        if command == "LOGIN":
            args = _split_args(rest)
            self.commands.append(f"LOGIN {_unquote(args[0]) if args else ''} ***")
        else:
            self.commands.append(f"{command} {rest}".strip())
        if command == "CAPABILITY":
            caps = "IMAP4" if "no_imap4rev1" in self.modes else "IMAP4rev1 AUTH=PLAIN"
            if "logindisabled" in self.modes:
                caps += " LOGINDISABLED"
            self._send(conn, f"* CAPABILITY {caps}\r\n".encode())
            self._ok(conn, tag)
        elif command == "NOOP":
            self._ok(conn, tag)
        elif command == "LOGOUT":
            self._send(conn, b"* BYE bye\r\n")
            self._ok(conn, tag)
            return False
        elif command == "LOGIN":
            args = [_unquote(a) for a in _split_args(rest)]
            if "logindisabled" in self.modes:
                self._no(conn, tag, "LOGIN disabled")
            elif len(args) == 2 and args[0] == self.user and args[1] == self.password:
                state["auth"] = True
                self.logins += 1
                self._ok(conn, tag, "logged in")
            else:
                self._no(conn, tag, "[AUTHENTICATIONFAILED] invalid credentials")
        elif command in ("LIST", "APPEND") and "drafts" in self.modes and state["auth"]:
            return self._drafts(conn, tag, command, rest, state)
        elif command in FORBIDDEN_COMMANDS:
            self.violations.append(command)
            self._no(conn, tag, "not allowed in this fake")
        elif not state["auth"]:
            self._send(conn, f"{tag} BAD log in first\r\n".encode())
        elif command == "STATUS":
            args = _split_args(rest)
            folder = self.folders.get(_unquote(args[0]))
            if folder is None:
                self._no(conn, tag, "no such mailbox")
                return True
            unseen = sum(1 for m in folder.messages if "\\Seen" not in m.flags)
            self._send(conn, (f'* STATUS "{folder.wire}" (MESSAGES {len(folder.messages)} UNSEEN {unseen} '
                              f"UIDNEXT {folder.uidnext} UIDVALIDITY {folder.uidvalidity})\r\n").encode())
            self._ok(conn, tag)
        elif command == "EXAMINE":
            folder = self.folders.get(_unquote(_split_args(rest)[0]))
            if folder is None:
                self._no(conn, tag, "no such mailbox")
                return True
            state["selected"] = folder
            self._send(conn, (f"* {len(folder.messages)} EXISTS\r\n* 0 RECENT\r\n"
                              f"* OK [UIDVALIDITY {folder.uidvalidity}] UIDs valid\r\n"
                              f"* OK [UIDNEXT {folder.uidnext}] next\r\n"
                              "* FLAGS (\\Seen \\Answered \\Flagged \\Deleted \\Draft)\r\n").encode())
            self._ok(conn, tag, "[READ-ONLY] EXAMINE completed")
        elif command == "UID":
            return self._uid(conn, tag, rest, state)
        else:
            self._send(conn, f"{tag} BAD unknown command\r\n".encode())
        return True

    def _drafts(self, conn: socket.socket, tag: str, command: str, rest: str, state: dict) -> bool:
        args = _split_args(rest)
        if command == "LIST":
            name = _unquote(args[-1]) if args else ""
            if name in self.folders:
                self._send(conn, f'* LIST (\\HasNoChildren \\Drafts) "/" "{name}"\r\n'.encode())
            self._ok(conn, tag)
            return True
        literal = re.search(r"\{(\d+)\}$", rest)
        folder = self.folders.get(_unquote(args[0])) if args else None
        if not literal:
            self._send(conn, f"{tag} BAD literal expected\r\n".encode())
            return True
        self._send(conn, b"+ go ahead\r\n")
        raw = state["reader"].read(int(literal.group(1)))
        state["reader"].readline()
        if folder is None:
            self._no(conn, tag, "[TRYCREATE] no such mailbox")
            return True
        flags = next((a for a in args if a.startswith("(")), "")
        uid = folder.uidnext
        folder.messages.append(Msg(uid, raw, set(flags.strip("()").split())))
        self.appended.append((_unquote(args[0]), flags, raw))
        self._ok(conn, tag, f"[APPENDUID {folder.uidvalidity} {uid}] appended")
        return True

    def _uid(self, conn: socket.socket, tag: str, rest: str, state: dict) -> bool:
        folder: Folder | None = state["selected"]
        sub, _, args = rest.partition(" ")
        sub = sub.upper()
        if folder is None:
            self._send(conn, f"{tag} BAD select first\r\n".encode())
            return True
        uids = [m.uid for m in folder.messages]
        if sub == "SEARCH":
            parts = args.split()
            chosen = uids
            if len(parts) == 2 and parts[0].upper() == "SINCE":
                day, mon, year = parts[1].split("-")
                since = datetime(int(year), MONTHS.index(mon) + 1, int(day)).date()
                chosen = [m.uid for m in folder.messages if self._date(m) >= since]
            self._send(conn, ("* SEARCH" + "".join(f" {u}" for u in chosen) + "\r\n").encode())
            self._ok(conn, tag)
            return True
        if sub != "FETCH":
            self.violations.append(f"UID {sub}")
            self._no(conn, tag)
            return True
        uid_text, _, items = args.partition(" ")
        upper = items.upper()
        if re.search(r"\b(?:BODY|BINARY)\[", upper) or re.search(r"\bRFC822(?!\.SIZE\b)", upper):
            self.violations.append(f"FETCH {items}")
            self._no(conn, tag, "would set \\Seen")
            return True
        if "malformed" in self.modes:
            self._send(conn, b"!!! garbage that is not IMAP\r\n")
            return True
        for seq, uid in ((uids.index(u) + 1, u) for u in _uid_set(uid_text, uids)):
            msg = folder.messages[seq - 1]
            if "unsolicited" in self.modes:
                evil = b"From: Bank Security <security@evil.example>\r\nSubject: urgent\r\n\r\n"
                self._send(conn, b"* 77 FETCH (UID 99999 BODY[HEADER.FIELDS (FROM SUBJECT)] {%d}\r\n%s)\r\n"
                           % (len(evil), evil))
                self._send(conn, b"* 78 FETCH (FLAGS (\\Seen))\r\n")
            parts = self._items(msg, upper)
            if "split" in self.modes and len(parts) > 2:
                first, second = parts[:2], parts[2:]
                self._send(conn, self._response(seq, [b"UID %d" % msg.uid] + [p for p in first if not p.startswith(b"UID ")]))
                self._send(conn, self._response(seq, [b"UID %d" % msg.uid] + [p for p in second if not p.startswith(b"UID ")]))
                continue
            if "reorder" in self.modes:
                parts = sorted(parts, key=lambda p: 0 if b"{" in p else 1)
            self._send(conn, self._response(seq, parts))
            if "conflict" in self.modes:
                # Disagrees with what the first response said for every FETCH we issue.
                self._send(conn, self._response(seq, [b"UID %d" % msg.uid, b"FLAGS (\\Deleted)",
                                                      b"RFC822.SIZE %d" % (len(msg.raw) + 1)]))
        self._ok(conn, tag)
        return True

    @staticmethod
    def _date(msg: Msg):
        day, mon, rest = msg.internaldate.split("-", 2)
        return datetime(int(rest[:4]), MONTHS.index(mon) + 1, int(day)).date()

    def _items(self, msg: Msg, upper: str) -> list[bytes]:
        parts: list[bytes] = []
        if re.search(r"\bUID\b", upper):
            parts.append(b"UID %d" % msg.uid)
        if "FLAGS" in upper:
            parts.append(b"FLAGS (" + " ".join(sorted(msg.flags)).encode() + b")")
        if "INTERNALDATE" in upper:
            parts.append(b'INTERNALDATE "' + msg.internaldate.encode() + b'"')
        if "RFC822.SIZE" in upper:
            parts.append(b"RFC822.SIZE %d" % len(msg.raw))
        header = re.search(r"BODY\.PEEK\[HEADER\.FIELDS \(([^)]*)\)\]", upper)
        if header:
            names = header.group(1).split()
            data = header_fields(msg.raw, names)
            if "giant_literal" in self.modes:
                data = b"X-Pad: " + b"a" * (2 * 1024 * 1024) + b"\r\n\r\n"
            parts.append(b"BODY[HEADER.FIELDS (" + header.group(1).encode() + b")] {%d}\r\n" % len(data) + data)
        body = re.search(r"BODY\.PEEK\[\]<0\.(\d+)>", upper)
        if body:
            data = msg.raw[: int(body.group(1))]
            parts.append(b"BODY[]<0> {%d}\r\n" % len(data) + data)
        return parts

    @staticmethod
    def _response(seq: int, parts: list[bytes]) -> bytes:
        return b"* %d FETCH (" % seq + b" ".join(parts) + b")\r\n"
