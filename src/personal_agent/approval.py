"""Approval before the assistant acts: levels, the broker and the console channel.

Claude Code asks our permission tool (`approval_server.py`, a separate process)
before it runs a tool that needs permission. That process forwards the request
over a Unix socket to the broker in the agent process. The broker

1. only reviews requests during an active turn, for a tool call the assistant
   really announced in that turn (same id, name and input),
2. denies, without asking, any path outside the working directory and any write
   to a protected path (.git, .claude, .mcp.json, ...),
3. classifies the tool into three levels:
   level 1 (read)    allowed without asking,
   level 2 (write)   the person must approve,
   level 3 (danger)  the person must approve and type a confirmation word;
   anything that cannot be classified safely is level 3,
4. asks the person through the active channel, one prompt at a time.

Every failure is a deny: no channel, timeout, bad token, unknown tool call,
too many pending prompts. The tool input is never modified; the permission
tool returns its own copy of it.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import select
import socket
import socketserver
import struct
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path, PurePath
from typing import Callable, Iterator, Protocol

from . import approval_server
from .approval_server import (
    ENV_SOCKET,
    ENV_TIMEOUT,
    ENV_TOKEN,
    MAX_FRAME_BYTES,
    PATH_KEYS,
    PROTOCOL,
    SERVER_NAME,
    TOOL_NAME,
    input_digest,
)
from .status import clean_text

try:
    import termios
except ImportError:  # pragma: no cover - not on Linux
    termios = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

PERMISSION_TOOL = f"mcp__{SERVER_NAME}__{TOOL_NAME}"
CONFIRM_WORD = "confirm"
DEFAULT_TIMEOUT_S = 300
BIND_WAIT_S = 2.0          # how long a request may wait for its tool_use event
MAX_WAITING = 3            # requests that may queue for the prompt
MAX_REFUSALS_PER_TURN = 2  # after this many refusals the rest of the turn is denied
MAX_TOOL_USES = 64         # announced tool calls remembered per turn
MAX_SOCKET_PATH = 100      # AF_UNIX allows 108 bytes
POLL_S = 0.5
CLOSE_JOIN_S = 3.0         # close() waits this long for running decisions to clean up
YES = ("y", "yes", "j", "ja")
SKILL_TOOL = "Skill"
SKILL_CONTEXT = "plugin skill in this turn"
CONTEXT_HINT = "this request may come from note text or a plugin skill"


class Level(IntEnum):
    READ = 1
    WRITE = 2
    DANGER = 3


LEVEL_LABELS = {Level.READ: "read", Level.WRITE: "write", Level.DANGER: "danger"}

TOOL_NAME_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
READ_TOOLS = frozenset({"Read", "Glob", "Grep"})
WRITE_TOOLS = frozenset({"Write", "Edit", "NotebookEdit"})
# Exact MCP tool names reviewed as read-only. Empty: --strict-mcp-config loads only our own server.
MCP_READ_ALLOWLIST: frozenset[str] = frozenset()
DANGER_WORDS = (
    "send", "delete", "pay", "publish", "post", "transfer", "remove", "order", "buy",
    "trash", "forward", "share", "submit", "purge", "erase", "destroy",
    "reply", "respond", "invite", "notify", "dispatch", "call", "dial", "sms",
    "grant", "revoke", "permission", "access", "member", "collaborator", "attendee", "public",
    "exec", "execute", "run", "eval", "script", "command", "install", "deploy", "push", "merge",
    "upload", "export", "sync", "webhook", "token", "secret", "password",
    "refund", "cancel", "book", "subscribe",
)
VERB_DANGER_WORDS = ("message",)  # dangerous only as the verb, e.g. message_user
READ_VERBS = ("read", "get", "list", "search", "find", "query", "view", "describe", "count", "lookup")
WRITE_VERBS = ("create", "write", "update", "edit", "add", "append", "rename", "save", "draft",
               "label", "mark", "tag")
RECIPIENT_KEYS = ("to", "cc", "bcc", "recipient", "recipients", "attendees", "invitees", "guests",
                  "members", "participants")
# Writes here are never allowed: they grant code execution or change the assistant's configuration.
PROTECTED_PARTS = (".git", ".claude", ".mcp.json", ".envrc", ".vscode", ".idea", ".claude.json")
# Writes here are level 3: persistent instructions for coding agents, CI workflows.
SENSITIVE_PARTS = PROTECTED_PARTS + ("claude.md", "claude.local.md", "agents.md", ".github")
EXTRA_PATH_KEYS = {"Glob": ("pattern",), "Grep": ("glob",)}

_CAMEL_LOWER = re.compile(r"([a-z0-9])([A-Z])")
_CAMEL_UPPER = re.compile(r"([A-Z]+)([A-Z][a-z])")
_SPLIT = re.compile(r"[_\-.:\s]+")
_PLAIN_KEY = re.compile(r"[A-Za-z0-9_]{1,40}")


# --- classification ------------------------------------------------------------


def tokens(name: str) -> list[str]:
    """`mcp__shop__buyItem` -> ["mcp", "shop", "buy", "item"]."""
    spaced = _CAMEL_LOWER.sub(r"\1 \2", _CAMEL_UPPER.sub(r"\1 \2", name))
    return [part.lower() for part in _SPLIT.split(spaced) if part]


def path_values(tool_name: str, tool_input: dict) -> Iterator[tuple[str, object]]:
    """All values of the input that name a file system location."""
    for key in PATH_KEYS + EXTRA_PATH_KEYS.get(tool_name, ()):
        if key in tool_input:
            yield key, tool_input[key]


def _relative_parts(value: str, workdir: Path | None) -> tuple[str, ...]:
    """Path components, relative to the working directory when one is given."""
    if workdir is None:
        return PurePath(value).parts
    root = Path(workdir).resolve()
    full = (root / Path(value).expanduser()).resolve()
    try:
        return full.relative_to(root).parts
    except ValueError:
        return full.parts


def _touches(tool_name: str, tool_input: dict, workdir: Path | None, names: tuple[str, ...]) -> bool:
    for _key, value in path_values(tool_name, tool_input):
        if not isinstance(value, str):
            return True
        try:
            parts = _relative_parts(value, workdir)
        except (OSError, ValueError, RuntimeError):
            return True
        if any(part.lower() in names for part in parts):
            return True
    return False


def _has_recipients(tool_input: dict) -> bool:
    return any(tool_input.get(key) for key in RECIPIENT_KEYS)


def classify(tool_name: object, tool_input: object, workdir: Path | None = None) -> Level:
    """Level of a tool call. Unknown or unclear means level 3."""
    if not isinstance(tool_name, str) or not tool_name or not isinstance(tool_input, dict):
        return Level.DANGER
    if not TOOL_NAME_RE.fullmatch(tool_name):
        return Level.DANGER
    if any(word.startswith(DANGER_WORDS) for word in tokens(tool_name)):
        return Level.DANGER
    if tool_name in READ_TOOLS:
        return Level.READ
    if tool_name in WRITE_TOOLS:
        return Level.DANGER if _touches(tool_name, tool_input, workdir, SENSITIVE_PARTS) else Level.WRITE
    if tool_name.startswith("mcp__"):
        server, sep, tool = tool_name[len("mcp__"):].partition("__")
        words = tokens(tool)
        if not (server and sep and words) or words[0].startswith(VERB_DANGER_WORDS):
            return Level.DANGER
        if _has_recipients(tool_input) or _touches(tool_name, tool_input, workdir, SENSITIVE_PARTS):
            return Level.DANGER
        if tool_name in MCP_READ_ALLOWLIST:
            return Level.READ
        # The name is chosen by the server: a read-looking name is never auto-allowed.
        if words[0] in READ_VERBS or words[0] in WRITE_VERBS:
            return Level.WRITE
        return Level.DANGER
    return Level.DANGER


def path_problem(tool_name: str, tool_input: dict, workdir: Path) -> str | None:
    """Reason to deny without asking: a path outside the working directory."""
    root = Path(workdir).resolve()
    for key, value in path_values(tool_name, tool_input):
        if not isinstance(value, str):
            return f"{key} is not a path"
        try:
            full = (root / Path(value).expanduser()).resolve()
        except (OSError, ValueError, RuntimeError):
            return "invalid path"
        if not full.is_relative_to(root):
            return "path outside the working directory"
    return None


def is_protected(tool_name: str, tool_input: dict, workdir: Path | None) -> bool:
    return _touches(tool_name, tool_input, workdir, PROTECTED_PARTS)


# --- rendering -----------------------------------------------------------------


def display_name(tool_name: object) -> str:
    if isinstance(tool_name, str) and TOOL_NAME_RE.fullmatch(tool_name):
        return tool_name
    return json.dumps(str(tool_name), ensure_ascii=True)[:80]


def _cut(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return f"{text[:limit]}…(+{len(text) - limit} chars)", True


def _show(value: object, limit: int) -> tuple[str, bool]:
    """JSON with ASCII escapes: control, bidi and look-alike characters become visible."""
    return _cut(json.dumps(value, ensure_ascii=True, default=str), limit)


def _show_key(key: object) -> tuple[str, bool]:
    key = str(key)
    return (key, False) if _PLAIN_KEY.fullmatch(key) else _show(key, 40)


def _render(tool_name: str, tool_input: object, limit: int) -> tuple[str, bool]:
    """Single-line rendering and whether anything was cut."""
    if not isinstance(tool_input, dict):
        return _show(tool_input, limit)
    pieces: list[str] = []
    cut = False
    done: set[str] = set()

    def prefix(key: str, chars: int, label: str) -> None:
        nonlocal cut
        value = tool_input.get(key)
        if not isinstance(value, str):
            return
        done.add(key)
        text = json.dumps(value[:chars], ensure_ascii=True)
        if len(value) > chars:
            text += f"…(+{len(value) - chars} chars)"
            cut = True
        pieces.append(f"{label}{text}")

    if tool_name in ("Write", "Edit") and "file_path" in tool_input:
        text, was_cut = _show(tool_input["file_path"], 200)
        pieces.append(f"file_path={text}")
        done.add("file_path")
        cut |= was_cut
    if tool_name == "Write" and isinstance(tool_input.get("content"), str):
        prefix("content", 160, f"content={len(tool_input['content'])} chars: ")
    if tool_name == "Edit":
        prefix("old_string", 80, "old=")
        prefix("new_string", 80, "new=")
    rest = [(k, v) for k, v in tool_input.items() if k not in done]
    room = max(0, 6 - len(pieces))
    for key, value in rest[:room]:
        key_text, key_cut = _show_key(key)
        text, was_cut = _show(value, 80)
        pieces.append(f"{key_text}={text}")
        cut |= was_cut or key_cut
    if len(rest) > room:
        pieces.append(f"(+{len(rest) - room} more keys)")
        cut = True
    line, was_cut = _cut(", ".join(pieces), limit)
    return line, cut or was_cut


def render_input(tool_name: str, tool_input: object, limit: int = 400) -> str:
    """Short single-line rendering for a prompt; every cut is marked."""
    return _render(tool_name, tool_input, limit)[0]


def render_full(tool_input: object) -> str:
    """The complete input, ASCII-escaped, for level-3 prompts that would otherwise be cut."""
    return json.dumps(tool_input, ensure_ascii=True, default=str)


# --- requests, verdicts and channels -------------------------------------------


@dataclass(frozen=True)
class ApprovalRequest:
    tool_name: str          # display_name(name)
    level: Level
    summary: str            # render_input(...)
    tool_use_id: str | None
    full: str | None = None  # complete input, set for level 3 when the summary is cut
    context: str | None = None  # e.g. "note context in this turn": the request may stem from note text
    request_id: str = field(default_factory=lambda: secrets.token_urlsafe(8))  # log reference only


@dataclass(frozen=True)
class Verdict:
    allow: bool
    message: str


class ApprovalChannel(Protocol):
    def ask(self, request: ApprovalRequest, deadline: float) -> bool:
        """True only for an explicit approval (incl. the level-3 confirmation).
        deadline is time.monotonic() based; must return by then."""
        ...


def read_stdin_line(timeout_s: float) -> str | None:
    """One line from stdin; None on timeout. Raises EOFError at end of input."""
    ready, _, _ = select.select([sys.stdin], [], [], max(timeout_s, 0))
    if not ready:
        return None
    line = sys.stdin.readline()
    if line == "":
        raise EOFError
    return line.rstrip("\n")


def stdin_is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError, OSError):
        return False


def discard_typeahead() -> bool:
    """Throw away input typed before the prompt was shown. False if that is not possible."""
    if termios is None:
        return False
    try:
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except Exception:  # noqa: BLE001 - termios.error, OSError, ValueError
        return False
    return True


_TIMEOUT = object()
_STOPPED = object()


class ConsoleChannel:
    """Asks the person in the terminal while the main thread waits for the assistant."""

    def __init__(
        self,
        lock: threading.Lock,
        read_line: Callable[[float], str | None] | None = None,
        write: Callable[[str], None] = print,
        stop: Callable[[], bool] | None = None,
    ) -> None:
        self.lock = lock
        self._read_line = read_line
        self.write = write
        self._stop = stop or (lambda: False)

    def ask(self, request: ApprovalRequest, deadline: float) -> bool:
        while True:
            if self._stop():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if self.lock.acquire(timeout=min(POLL_S, remaining)):
                break
        try:
            return self._prompt(request, deadline)
        finally:
            self.lock.release()

    def _read(self, deadline: float) -> object:
        reader = self._read_line or read_stdin_line
        while True:
            if self._stop():
                return _STOPPED
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _TIMEOUT
            try:
                line = reader(min(POLL_S, remaining))
            except EOFError:
                return _STOPPED
            if line is not None:
                return line

    def _answer_ok(self, answer: object, accepted: tuple[str, ...]) -> bool:
        if answer is _TIMEOUT:
            self.write("[approval timed out: denied]")
            return False
        if not isinstance(answer, str) or answer.strip().lower() not in accepted:
            self.write("[denied]")
            return False
        return True

    def _prompt(self, request: ApprovalRequest, deadline: float) -> bool:
        if not stdin_is_tty():
            self.write(f"[approval for {request.tool_name} needs an interactive terminal: denied]")
            return False
        if not discard_typeahead():
            self.write("[could not clear typed-ahead input: denied]")
            return False
        self.write(f"[approval] level {int(request.level)} ({LEVEL_LABELS[request.level]}): {request.tool_name}")
        if request.context:
            self.write(f"  ({request.context}: {CONTEXT_HINT})")
        self.write(f"  {request.summary}")
        if request.full is not None:
            self.write("  complete input:")
            self.write(f"  {request.full}")
        self.write("Allow? [y/N]")
        if not self._answer_ok(self._read(deadline), YES):
            return False
        if request.level >= Level.DANGER:
            self.write(f'Type "{CONFIRM_WORD}" to allow this level-3 action:')
            if not self._answer_ok(self._read(deadline), (CONFIRM_WORD,)):
                return False
        self.write("[approved]")
        return True


# --- files and MCP configuration -----------------------------------------------


def build_mcp_config(socket_path: Path, token: str, timeout_s: int) -> dict:
    """MCP config for Claude Code. The child runs isolated from an absolute path.

    -I ignores PYTHON* variables, the cwd and the user site; -S also skips the `site`
    module, so no .pth file in any site-packages runs (the child needs only the stdlib).
    """
    return {
        "mcpServers": {
            SERVER_NAME: {
                "command": sys.executable,
                "args": ["-I", "-S", str(Path(approval_server.__file__).resolve())],
                "env": {ENV_SOCKET: str(socket_path), ENV_TOKEN: token, ENV_TIMEOUT: str(int(timeout_s))},
            }
        }
    }


def write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.chmod(path, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


# --- the broker ----------------------------------------------------------------


@dataclass(frozen=True)
class _Announced:
    name: str
    input: dict
    digest: str


def differing_keys(announced: dict, forwarded: dict) -> list[str]:
    """Top-level keys whose values differ between the stream and the permission request.

    The forwarded input is shrunk by the permission tool, so the announced value is
    shrunk the same way before comparing. A key that differs only beyond the shrink
    limits is not listed (the digest still catches it).
    """
    shrunk = approval_server.shrink_input(announced)
    keys = list(dict.fromkeys([*shrunk, *forwarded]))
    return [
        str(key) for key in keys
        if key not in shrunk or key not in forwarded or shrunk[key] != forwarded[key]
    ]


def _log_input_mismatch(tool_name: str, announced: dict, forwarded: dict) -> None:
    """Diagnostics for a binding failure: key names only, never values."""
    try:
        keys = differing_keys(announced, forwarded)
    except Exception as exc:  # noqa: BLE001 - diagnostics must never change the verdict
        keys = [f"<{type(exc).__name__}>"]
    shown = ", ".join(clean_text(key, 40) for key in keys[:10]) or "none visible after shrinking"
    log.warning("approval input of %s differs from the announced tool call; keys: %s", clean_text(tool_name, 80), shown)


def _peer_is_us(conn: socket.socket) -> bool:
    option = getattr(socket, "SO_PEERCRED", None)
    if option is None:
        return False
    try:
        data = conn.getsockopt(socket.SOL_SOCKET, option, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", data)
    except (OSError, struct.error):
        return False
    return uid == os.getuid()


class _Handler(socketserver.StreamRequestHandler):
    """One request per connection, from the permission tool process."""

    timeout = 5

    def handle(self) -> None:
        try:
            verdict = self._verdict()
        except Exception as exc:  # noqa: BLE001 - e.g. RecursionError from a deeply nested frame
            if not isinstance(exc, (OSError, ValueError)):
                log.warning("invalid approval request: %s", type(exc).__name__)
            verdict = Verdict(False, "invalid request")
        reply = json.dumps({"v": PROTOCOL, "allow": verdict.allow, "message": verdict.message}) + "\n"
        try:
            self.wfile.write(reply.encode("utf-8"))
        except OSError:
            pass

    def _verdict(self) -> Verdict:
        broker: ApprovalBroker = self.server.broker  # type: ignore[attr-defined]
        if not _peer_is_us(self.connection):
            log.warning("approval request from another user refused")
            return Verdict(False, "unauthorized")
        raw = self.rfile.readline(MAX_FRAME_BYTES + 1)
        if not raw.endswith(b"\n") or len(raw) > MAX_FRAME_BYTES:
            return Verdict(False, "invalid request")
        frame = json.loads(raw)
        if not isinstance(frame, dict) or frame.get("v") != PROTOCOL:
            return Verdict(False, "invalid request")
        token = frame.get("token")
        expected = broker.token
        if not isinstance(token, str) or expected is None or not hmac.compare_digest(
            token.encode("utf-8"), expected.encode("utf-8")
        ):
            log.warning("approval request with a wrong token refused")
            return Verdict(False, "unauthorized")
        tool_name = frame.get("tool_name")
        if not isinstance(tool_name, str):
            return Verdict(False, "invalid request")
        tool_use_id = frame.get("tool_use_id")
        digest = frame.get("input_sha256")
        return broker.decide(
            tool_name,
            frame.get("input"),
            tool_use_id if isinstance(tool_use_id, str) else None,
            digest if isinstance(digest, str) else "",
        )


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    block_on_close = False

    def handle_error(self, request, client_address) -> None:  # noqa: ANN001
        # Never print a traceback into the chat terminal, possibly over an approval prompt.
        log.warning("approval handler failed: %s", type(sys.exc_info()[1]).__name__)


class ApprovalBroker:
    """Receives permission requests on a Unix socket and decides them."""

    def __init__(
        self,
        workdir: Path,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        channel: ApprovalChannel | None = None,
        clock: Callable[[], float] = time.monotonic,
        bind_wait_s: float = BIND_WAIT_S,
    ) -> None:
        self.workdir = Path(workdir)
        self.timeout_s = timeout_s
        self.channel = channel  # replaceable at runtime
        self.clock = clock
        self.bind_wait_s = bind_wait_s
        self.allowed_skills: frozenset[str] = frozenset()  # "<plugin>:<name>" of loaded plugins
        self.token: str | None = None
        self.turn_active = threading.Event()
        self.closing = threading.Event()
        self._prompt_lock = threading.Lock()
        self._cond = threading.Condition()
        self._tool_uses: OrderedDict[str, _Announced] = OrderedDict()
        self._used: set[str] = set()
        self._waiting = 0
        self._inflight = 0
        self._refusals = 0
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self._dir: Path | None = None
        self._runtime_files: list[str] = []
        self._turn_context: str | None = None

    # --- turn bookkeeping, called by the chat loop ---

    def begin_turn(self) -> None:
        with self._cond:
            self._tool_uses.clear()
            self._used.clear()
            self._refusals = 0
            self._turn_context = None
            self.turn_active.set()
            self._cond.notify_all()

    def end_turn(self) -> None:
        with self._cond:
            self.turn_active.clear()
            self._tool_uses.clear()
            self._cond.notify_all()

    def set_turn_context(self, note: str) -> None:
        """Mark the active turn, e.g. because note excerpts were attached. Reset by begin_turn."""
        note = clean_text(note, 60)
        with self._cond:
            current = self._turn_context
            if current and note and note not in current:
                note = f"{current}; {note}"
            self._turn_context = clean_text(note, 120) or current

    def observe(self, event: object) -> None:
        """Remember the tool calls the assistant announces in the stream (assistant events)."""
        if not isinstance(event, dict) or event.get("type") != "assistant":
            return
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return
        with self._cond:
            if not self.turn_active.is_set():
                return
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                tool_use_id, name, tool_input = block.get("id"), block.get("name"), block.get("input")
                if not (isinstance(tool_use_id, str) and tool_use_id and isinstance(name, str)
                        and isinstance(tool_input, dict)):
                    continue
                if tool_use_id in self._tool_uses:
                    continue  # the first announcement wins
                self._tool_uses[tool_use_id] = _Announced(name, tool_input, input_digest(tool_input))
                while len(self._tool_uses) > MAX_TOOL_USES:
                    self._tool_uses.popitem(last=False)
            self._cond.notify_all()

    # --- decision ---

    def decide(
        self,
        tool_name: str,
        tool_input: object,
        tool_use_id: str | None = None,
        input_sha256: str | None = None,
    ) -> Verdict:
        request_id = secrets.token_urlsafe(6)
        level: Level | None = None
        with self._cond:
            self._inflight += 1
        try:
            verdict, level, request_id = self._decide(tool_name, tool_input, tool_use_id, input_sha256)
        except Exception as exc:  # noqa: BLE001 - every failure is a deny
            log.warning("approval failed: %s", type(exc).__name__)
            verdict = Verdict(False, "approval failed")
        finally:
            with self._cond:
                self._inflight -= 1
                self._cond.notify_all()
        log.info(
            "approval %s %s level %s -> %s",
            request_id,
            clean_text(tool_name, 80),
            int(level) if level is not None else "-",
            "allow" if verdict.allow else "deny",
        )
        return verdict

    def _match(self, tool_name: str, tool_input: dict, tool_use_id: str | None, digest: str) -> tuple[dict | None, str]:
        """The announced input of this tool call, or the reason there is none."""
        if not tool_use_id:
            return None, "no matching tool call"
        end = time.monotonic() + self.bind_wait_s
        with self._cond:
            while True:
                if not self.turn_active.is_set():
                    return None, "no active turn"
                entry = self._tool_uses.get(tool_use_id)
                if entry is not None:
                    break
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return None, "no matching tool call"
                self._cond.wait(remaining)
            if tool_use_id in self._used:
                return None, "this tool call was already reviewed"
            if entry.name != tool_name:
                return None, "no matching tool call"
            if not hmac.compare_digest(entry.digest, digest):
                _log_input_mismatch(tool_name, entry.input, tool_input)
                return None, "input differs from the announced tool call"
            for key in PATH_KEYS:
                if tool_input.get(key) != entry.input.get(key):
                    _log_input_mismatch(tool_name, entry.input, tool_input)
                    return None, "input differs from the announced tool call"
            self._used.add(tool_use_id)
            return entry.input, ""

    def _acquire_prompt(self, deadline: float) -> bool:
        while not self.closing.is_set():
            remaining = deadline - self.clock()
            if remaining <= 0:
                return False
            if self._prompt_lock.acquire(timeout=min(POLL_S, remaining)):
                return True
        return False

    def _refuse(self, message: str) -> Verdict:
        with self._cond:
            self._refusals += 1
        return Verdict(False, message)

    def _decide(
        self, tool_name: str, tool_input: object, tool_use_id: str | None, digest: str | None
    ) -> tuple[Verdict, Level | None, str]:
        rid = secrets.token_urlsafe(6)
        if tool_name == PERMISSION_TOOL:
            return Verdict(False, "the approval tool cannot be used directly"), None, rid
        if self.closing.is_set():
            return Verdict(False, "the approval service is closing"), None, rid
        if not self.turn_active.is_set():
            return Verdict(False, "no active turn"), None, rid
        if not isinstance(tool_name, str) or not isinstance(tool_input, dict):
            return Verdict(False, "invalid request"), None, rid
        deadline = self.clock() + self.timeout_s
        if digest is None:
            digest = input_digest(tool_input)
        announced, reason = self._match(tool_name, tool_input, tool_use_id, digest)
        if announced is None:
            return Verdict(False, reason), None, rid
        reason = path_problem(tool_name, announced, self.workdir)
        if reason:
            return Verdict(False, reason), None, rid
        if tool_name == SKILL_TOOL:
            # The bridge routes every Skill call here (permissions "ask"). Only the
            # skills of the checked, loaded plugins are allowed, without a prompt;
            # built-in skills (update-config, schedule, ...) are denied without asking.
            skill = announced.get("skill")
            if isinstance(skill, str) and skill.lstrip("/") in self.allowed_skills:
                self.set_turn_context(SKILL_CONTEXT)
                return Verdict(True, "allowed plugin skill"), Level.READ, rid
            return Verdict(False, "this skill is not allowed"), None, rid
        # classify() itself stays fail-safe: any other route of Skill here is level 3.
        level = classify(tool_name, announced, self.workdir)
        if level is Level.READ:
            return Verdict(True, "read-only"), level, rid
        if is_protected(tool_name, announced, self.workdir):
            return Verdict(False, "protected path: the assistant may not change it"), level, rid

        summary, was_cut = _render(tool_name, announced, 400)
        request = ApprovalRequest(
            tool_name=display_name(tool_name),
            level=level,
            summary=summary,
            tool_use_id=tool_use_id,
            full=render_full(announced) if level >= Level.DANGER and was_cut else None,
            context=self._turn_context,
        )
        with self._cond:
            if self._refusals >= MAX_REFUSALS_PER_TURN:
                return Verdict(False, "too many refused approvals in this turn"), level, request.request_id
            if self._waiting >= MAX_WAITING:
                return Verdict(False, "too many pending approvals"), level, request.request_id
            self._waiting += 1
        try:
            acquired = self._acquire_prompt(deadline)
        finally:
            with self._cond:
                self._waiting -= 1
        if not acquired:
            return Verdict(False, "approval timed out"), level, request.request_id
        try:
            with self._cond:
                tripped = self._refusals >= MAX_REFUSALS_PER_TURN
            if tripped:
                return Verdict(False, "too many refused approvals in this turn"), level, request.request_id
            if self.closing.is_set() or not self.turn_active.is_set():
                return Verdict(False, "no active turn"), level, request.request_id
            channel = self.channel
            if channel is None:
                return Verdict(False, "no approval channel is available"), level, request.request_id
            try:
                approved = channel.ask(request, deadline) is True
            except Exception as exc:  # noqa: BLE001
                log.warning("approval channel failed: %s", type(exc).__name__)
                approved = False
            if self.clock() > deadline:
                return self._refuse("approval timed out"), level, request.request_id
            if not approved:
                return self._refuse("the person did not approve this action"), level, request.request_id
            return Verdict(True, "approved by the person"), level, request.request_id
        finally:
            self._prompt_lock.release()

    # --- socket lifecycle ---

    @property
    def runtime_dir(self) -> Path | None:
        return self._dir

    @property
    def socket_path(self) -> Path | None:
        return self._dir / "s" if self._dir else None

    @property
    def mcp_config_path(self) -> Path | None:
        return self._dir / "mcp.json" if self._dir else None

    def start(self, runtime_parent: Path | None) -> Path:
        """Listen on a private Unix socket and write the MCP config. Returns its path."""
        if self._server is not None:
            raise RuntimeError("broker already started")
        parent = None
        if runtime_parent is not None:
            runtime_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            parent = str(runtime_parent)
        directory = Path(tempfile.mkdtemp(prefix="appr-", dir=parent))
        if len(os.fsencode(directory / "s")) > MAX_SOCKET_PATH:
            os.rmdir(directory)
            directory = Path(tempfile.mkdtemp(prefix="appr-"))
        os.chmod(directory, 0o700)
        self._dir = directory
        try:
            self.token = secrets.token_urlsafe(32)
            sock_path = directory / "s"
            server = _Server(str(sock_path), _Handler)
            server.broker = self  # type: ignore[attr-defined]
            os.chmod(sock_path, 0o600)
            self._server = server
            write_private(directory / "mcp.json", json.dumps(build_mcp_config(sock_path, self.token, self.timeout_s)))
            self._thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": POLL_S}, daemon=True
            )
            self._thread.start()
        except BaseException:
            self.close()
            raise
        return directory / "mcp.json"

    def write_runtime_file(self, name: str, text: str) -> Path:
        """Write a private (0600) file into the runtime directory; close() removes it."""
        if self._dir is None:
            raise RuntimeError("broker not started")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name) or name in ("s", "mcp.json"):
            raise ValueError("invalid runtime file name")
        path = self._dir / name
        write_private(path, text)
        self._runtime_files.append(name)
        return path

    def close(self, join_s: float = CLOSE_JOIN_S) -> None:
        """Stop listening and remove the socket, the MCP config and the directory.

        Running decisions see `closing` and stop; they get up to join_s to finish,
        so a channel can still clean up (e.g. cancel a pending remote approval).
        """
        self.closing.set()
        self.end_turn()
        end = time.monotonic() + max(0.0, join_s)
        with self._cond:
            while self._inflight > 0:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
        server, self._server = self._server, None
        thread, self._thread = self._thread, None
        if server is not None:
            if thread is not None:  # shutdown() blocks forever if serve_forever never ran
                server.shutdown()
            server.server_close()
        directory, self._dir = self._dir, None
        if directory is not None:
            for name in ("s", "mcp.json", *self._runtime_files):
                try:
                    (directory / name).unlink()
                except OSError:
                    pass
            self._runtime_files.clear()
            try:
                directory.rmdir()
            except OSError:
                pass
