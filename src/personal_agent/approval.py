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

One broker serves the whole chat. Every session gets its own *scope*: the main
session ("chat") and each background helper ("task <id>"). A scope has its own
token, MCP config file and turn state, so a request is bound to a tool call that
was announced in the stream of that very session. All scopes share one prompt
queue: one prompt at a time, the main session first. Helpers may write only an
append to their own domain notes; everything else they try to write is denied
without asking.
"""

from __future__ import annotations

import contextlib
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
from typing import TYPE_CHECKING, Callable, Iterator, Protocol

from . import approval_server
from .approval_server import (
    ENV_DELEGATE,
    ENV_SOCKET,
    ENV_TIMEOUT,
    ENV_TOKEN,
    MAX_DELEGATE_TEXT,
    MAX_FRAME_BYTES,
    PATH_KEYS,
    PROTOCOL,
    SERVER_NAME,
    TOOL_NAME,
    input_digest,
)
from .bridge import DELEGATE_TOOL
from .status import clean_text
from .textsafe import strip_invisible
from .vault import TASK_ID_RE, Vault, valid_domain

if TYPE_CHECKING:
    from .console import ChatConsole

try:
    import termios
except ImportError:  # pragma: no cover - not on Linux
    termios = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

PERMISSION_TOOL = f"mcp__{SERVER_NAME}__{TOOL_NAME}"
CONFIRM_WORD = "confirm"
DEFAULT_TIMEOUT_S = 300
BIND_WAIT_S = 2.0          # how long a request may wait for its tool_use event
MAX_WAITING = 3            # requests of one scope that may queue for the prompt
MAX_WAITING_TOTAL = 6      # requests of all scopes together
MAX_REFUSALS_PER_TURN = 2  # after this many refusals the rest of the scope's turn is denied
MAX_PROMPTS_PER_HELPER_TASK = 3  # prompts a helper may show in its whole task (timeouts count)
HELPER_PROMPT_TIMEOUT_S = 120    # a helper prompt never blocks the terminal longer than this
MAX_DELEGATIONS_PER_TURN = 1
MAX_SCOPES = 4             # main, two helpers, one spare for a scope that is closing
MAX_TOOL_USES = 64         # announced tool calls remembered per turn
MAX_SOCKET_PATH = 100      # AF_UNIX allows 108 bytes
POLL_S = 0.5
CLOSE_JOIN_S = 3.0         # close() waits this long for running decisions to clean up
MAIN_LABELS = ("chat", "telegram")  # the main scope's trusted label: the terminal chat or serve's Telegram assistant
YES = ("y", "yes", "j", "ja")
SKILL_TOOL = "Skill"
SKILL_CONTEXT = "plugin skill in this turn"
HELPER_OUTPUT_CONTEXT = "helper output read in this turn"
HELPER_TASK_CONTEXT = "helper task"
CONTEXT_HINT = "this request may come from note text or a plugin skill"
SCOPE_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")
# Helper-written data: reading it marks the main turn, and the main session never writes task results.
HELPER_DATA_PARTS = (("outputs", "tasks"), ("wiki", "domains"))
TASK_RESULT_PARTS = ("outputs", "tasks")
MAX_DOMAIN_APPEND_CHARS = 600
MAX_DOMAIN_APPEND_LINES = 3
MAX_APPEND_SUMMARY = 1600  # the remote page accepts 2000 characters including the header lines
MAX_DOMAIN_FILE_BYTES = 128 * 1024  # same cap as domains.MAX_DOMAIN_READ; a larger file is not appended to


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
    if tool_name == DELEGATE_TOOL:
        # Our own tool; it only queues a task. The broker raises it to level 3 when the
        # turn carries note or skill context (see _decide_delegate).
        return Level.WRITE
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


def _lower_parts(value: object, workdir: Path) -> tuple[str, ...] | None:
    """Lowercased path components relative to workdir; None when unusable."""
    if not isinstance(value, str):
        return None
    try:
        return tuple(part.lower() for part in _relative_parts(value, workdir))
    except (OSError, ValueError, RuntimeError):
        return None


def _under(parts: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    return parts[: len(prefix)] == prefix


def writes_under(tool_name: str, tool_input: dict, workdir: Path, prefix: tuple[str, ...]) -> bool:
    """Whether any path of the call lies under prefix (a component prefix, not any component)."""
    for _key, value in path_values(tool_name, tool_input):
        parts = _lower_parts(value, workdir)
        if parts is None or _under(parts, prefix):
            return True
    return False


def reads_helper_data(tool_name: str, tool_input: dict, workdir: Path) -> bool:
    """Whether a read-level call may read helper-written files (task results, domain notes).

    Grep and Glob without a path search the whole vault; a path that is a parent of a
    helper-data folder (e.g. "outputs" or ".") reaches it too.
    """
    values = [value for key, value in path_values(tool_name, tool_input) if key in PATH_KEYS]
    if tool_name in ("Grep", "Glob") and not values:
        return True
    for value in values:
        parts = _lower_parts(value, workdir)
        if parts is None:
            return True
        for prefix in HELPER_DATA_PARTS:
            if _under(parts, prefix) or _under(prefix, parts):
                return True
    return False


def delegate_input_problem(tool_input: object) -> str | None:
    """Why a delegate_task input is refused without asking, or None."""
    from .router import strip_leading  # router is not needed at import time

    if not isinstance(tool_input, dict) or not set(tool_input) <= {"text", "domain"}:
        return "invalid delegation request"
    text = tool_input.get("text")
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= MAX_DELEGATE_TEXT:
        return f"a delegated task needs 1 to {MAX_DELEGATE_TEXT} characters of text"
    if strip_leading(text).startswith("/"):
        return "a task must not start with /"
    if "domain" in tool_input and not valid_domain(tool_input["domain"]):
        return "invalid domain name"
    return None


def domain_append_problem(tool_input: dict, task_id: str | None) -> tuple[str | None, str]:
    """(why an Edit of the domain file is refused, the appended text).

    Only a pure append is accepted: new_string = old_string + up to three lines of the
    form "- YYYY-MM-DD (task <own id>): finding", at most 600 characters, no invisible
    characters, no heading, Links line or checkbox.
    """
    old, new = tool_input.get("old_string"), tool_input.get("new_string")
    if tool_input.get("replace_all"):
        return "domain notes are append-only", ""
    if not isinstance(old, str) or not isinstance(new, str) or not old or not new.startswith(old) or new == old:
        return "domain notes are append-only", ""
    appended = new[len(old):]
    if len(appended) > MAX_DOMAIN_APPEND_CHARS:
        return f"a domain note may add at most {MAX_DOMAIN_APPEND_CHARS} characters", appended
    if not old.endswith("\n") and not appended.startswith("\n"):
        return "a domain note must start on a new line", appended
    if strip_invisible(appended, keep="\n") != appended or "\r" in appended:
        return "a domain note must not contain control or invisible characters", appended
    lines = appended.strip("\n").split("\n")
    if not lines or len(lines) > MAX_DOMAIN_APPEND_LINES:
        return f"a domain note may add at most {MAX_DOMAIN_APPEND_LINES} lines", appended
    if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
        return "domain notes are append-only", appended
    pattern = re.compile(r"- \d{4}-\d{2}-\d{2} \(task " + task_id + r"\): [^\n]{1,300}")
    for line in lines:
        if line.startswith(("Links:", "#", "- [")) or not pattern.fullmatch(line):
            return f'each domain note line must look like "- YYYY-MM-DD (task {task_id}): finding"', appended
    return None, appended


def domain_append_location_problem(content: str | None, old: str) -> str | None:
    """Why an Edit with old_string is not an append at the end of the Notes section, or None.

    Claude Code's Edit replaces the first occurrence of old_string anywhere in the file;
    new_string = old_string + lines is an append only when old_string is the file's
    tail, occurs nowhere else, and does not reach back before the "## Notes" heading.
    Then the result is exactly content + the appended lines.
    """
    if content is None:
        return "the domain file is missing or unreadable"
    if not old or content.find(old) != len(content) - len(old):
        return "a domain note must be appended at the end of the file"
    heading = None
    offset = 0
    for line in content.splitlines(keepends=True):
        if strip_invisible(line, keep="\t").strip().lower() == "## notes":
            heading = offset
            break
        offset += len(line)
    if heading is None:
        return 'the domain file has no "## Notes" heading'
    if len(content) - len(old) < heading:
        return 'a domain note must be appended under "## Notes"'
    return None


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
    scope: str = "main"         # the scope name, for request_stopped
    scope_label: str = "chat"   # trusted: "chat" or "task 3f2a9c (research)", never built from free text
    origin: str | None = None   # a helper task's title, quoted and ASCII-escaped


@dataclass(frozen=True)
class Verdict:
    allow: bool
    message: str
    task_id: str | None = None  # set when a delegation queued a task


def approval_header(request: ApprovalRequest) -> str:
    """The first line of every prompt: who asks, the level and the tool."""
    return (f"[approval · {request.scope_label}] level {int(request.level)} "
            f"({LEVEL_LABELS[request.level]}): {request.tool_name}")


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
    """Asks the person in the terminal, for the main session and for helpers.

    stop() ends every prompt (the broker is closing); stop_for(request) ends one
    prompt (its scope's turn ended, or the main session needs the terminal). With a
    ChatConsole, the idle `you>` prompt yields the terminal while a prompt waits.
    """

    def __init__(
        self,
        lock: threading.Lock,
        read_line: Callable[[float], str | None] | None = None,
        write: Callable[[str], None] = print,
        stop: Callable[[], bool] | None = None,
        stop_for: Callable[[ApprovalRequest], bool] | None = None,
        console: "ChatConsole | None" = None,
        preempted: Callable[[ApprovalRequest], bool] | None = None,
    ) -> None:
        self.lock = lock
        self._read_line = read_line
        self.write = write
        self._stop = stop or (lambda: False)
        self._stop_for = stop_for
        self.console = console
        self._preempted = preempted  # broker.request_preempted: say "withdrawn", not "denied"

    def _stopped(self, request: ApprovalRequest) -> bool:
        return self._stop() or (self._stop_for is not None and self._stop_for(request))

    def ask(self, request: ApprovalRequest, deadline: float) -> bool:
        wanted = self.console.wanted() if self.console is not None else contextlib.nullcontext()
        with wanted:
            while True:
                if self._stopped(request):
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

    def _read(self, deadline: float, request: ApprovalRequest) -> object:
        reader = self._read_line or read_stdin_line
        while True:
            if self._stopped(request):
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

    def _answer_ok(self, answer: object, accepted: tuple[str, ...], request: ApprovalRequest | None = None) -> bool:
        if answer is _TIMEOUT:
            self.write("[approval timed out: denied]")
            return False
        if (answer is _STOPPED and request is not None and self._preempted is not None
                and self._preempted(request)):
            self.write("[withdrawn: the chat needs the prompt]")
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
        if self.console is not None and self.console.reading:
            self.write("")
            self.write(f"[approval request from {request.scope_label}: anything you were typing was discarded]")
        self.write(approval_header(request))
        if request.origin:
            self.write(f"  task: {request.origin}")
        if request.context:
            self.write(f"  ({request.context}: {CONTEXT_HINT})")
        self.write(f"  {request.summary}")
        if request.full is not None:
            self.write("  complete input:")
            self.write(f"  {request.full}")
        self.write("Allow? [y/N]")
        if not self._answer_ok(self._read(deadline, request), YES, request):
            return False
        if request.level >= Level.DANGER:
            self.write(f'Type "{CONFIRM_WORD}" to allow this level-3 action:')
            if not self._answer_ok(self._read(deadline, request), (CONFIRM_WORD,), request):
                return False
        self.write("[approved]")
        return True


# --- files and MCP configuration -----------------------------------------------


def build_mcp_config(socket_path: Path, token: str, timeout_s: int, delegate: bool = False) -> dict:
    """MCP config for Claude Code. The child runs isolated from an absolute path.

    -I ignores PYTHON* variables, the cwd and the user site; -S also skips the `site`
    module, so no .pth file in any site-packages runs (the child needs only the stdlib).
    With delegate, the server also offers delegate_task (main session only).
    """
    env = {ENV_SOCKET: str(socket_path), ENV_TOKEN: token, ENV_TIMEOUT: str(int(timeout_s))}
    if delegate:
        env[ENV_DELEGATE] = "1"
    return {
        "mcpServers": {
            SERVER_NAME: {
                "command": sys.executable,
                "args": ["-I", "-S", str(Path(approval_server.__file__).resolve())],
                "env": env,
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
    """One request per connection, from a permission tool process (any scope)."""

    timeout = 5

    def handle(self) -> None:
        try:
            verdict = self._verdict()
        except Exception as exc:  # noqa: BLE001 - e.g. RecursionError from a deeply nested frame
            if not isinstance(exc, (OSError, ValueError)):
                log.warning("invalid approval request: %s", type(exc).__name__)
            verdict = Verdict(False, "invalid request")
        reply: dict = {"v": PROTOCOL, "allow": verdict.allow, "message": verdict.message}
        if verdict.task_id is not None:
            reply["task_id"] = verdict.task_id
        try:
            self.wfile.write((json.dumps(reply) + "\n").encode("utf-8"))
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
        scope = broker._scope_for_token(token) if isinstance(token, str) else None
        if scope is None:
            log.warning("approval request with a wrong token refused")
            return Verdict(False, "unauthorized")
        op = frame.get("op", "approve")
        if op == "delegate":
            return broker.delegate(scope, frame)
        if op != "approve":
            return Verdict(False, "invalid request")
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
            scope=scope,
        )


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    block_on_close = False

    def handle_error(self, request, client_address) -> None:  # noqa: ANN001
        # Never print a traceback into the chat terminal, possibly over an approval prompt.
        log.warning("approval handler failed: %s", type(sys.exc_info()[1]).__name__)


# --- scopes and the prompt queue -----------------------------------------------------


@dataclass(eq=False)
class ApprovalScope:
    """One session's view of the broker: its token, MCP config and turn state.

    The turn state (announced tool calls, refusals, context, delegations) is guarded
    by the broker's condition; the methods delegate to the broker.
    """

    name: str                      # "main" or "task-<id>"
    kind: str                      # "main" | "helper"
    label: str                     # trusted prompt label: "chat" or "task 3f2a9c (research)"
    token: str = field(repr=False)
    origin: str | None = None      # helper: the task title, quoted and ASCII-escaped
    task_id: str | None = None
    domain_file: str | None = None  # helper with a domain: "wiki/domains/<domain>.md"
    allowed_skills: frozenset[str] = frozenset()
    write_deny_parts: tuple[tuple[str, ...], ...] = ()
    delegation: bool = False       # main only, PA_TASK_DELEGATION=on
    mcp_config_path: Path | None = None
    turn_active: threading.Event = field(default_factory=threading.Event)
    broker: "ApprovalBroker | None" = field(default=None, repr=False)
    _tool_uses: OrderedDict = field(default_factory=OrderedDict, repr=False)
    _used: set = field(default_factory=set, repr=False)
    _refusals: int = field(default=0, repr=False)
    _turn_context: str | None = field(default=None, repr=False)
    _waiting: int = field(default=0, repr=False)
    _prompting: bool = field(default=False, repr=False)
    _prompts_shown: int = field(default=0, repr=False)
    _approved_delegations: list = field(default_factory=list, repr=False)
    _delegations: int = field(default=0, repr=False)

    def begin_turn(self) -> None:
        assert self.broker is not None
        self.broker._begin_turn(self)

    def end_turn(self) -> None:
        assert self.broker is not None
        self.broker._end_turn(self)

    def observe(self, event: object) -> None:
        assert self.broker is not None
        self.broker._observe(self, event)

    def set_turn_context(self, note: str) -> None:
        assert self.broker is not None
        self.broker._set_turn_context(self, note)


class _PromptQueue:
    """One prompt at a time; the lowest (priority, arrival) waiting ticket goes next.

    Priority 0 is the main session, 1 a helper. Shares the broker's condition.
    """

    def __init__(self, cond: threading.Condition) -> None:
        self._cond = cond
        self._holder: tuple[int, int] | None = None
        self._tickets: list[tuple[int, int]] = []
        self._seq = 0

    def acquire(self, priority: int, deadline: float, clock: Callable[[], float],
                closing: threading.Event, stopped: Callable[[], bool]) -> bool:
        with self._cond:
            self._seq += 1
            ticket = (priority, self._seq)
            self._tickets.append(ticket)
            self._cond.notify_all()
            try:
                while True:
                    if closing.is_set() or stopped():
                        return False
                    if self._holder is None and ticket == min(self._tickets):
                        self._tickets.remove(ticket)
                        self._holder = ticket
                        return True
                    remaining = deadline - clock()
                    if remaining <= 0:
                        return False
                    self._cond.wait(min(POLL_S, remaining))
            finally:
                if ticket in self._tickets:
                    self._tickets.remove(ticket)
                    self._cond.notify_all()

    def release(self) -> None:
        with self._cond:
            self._holder = None
            self._cond.notify_all()

    def main_waiting(self) -> bool:
        with self._cond:
            return any(priority == 0 for priority, _ in self._tickets)


def _quoted_title(title: str | None) -> str | None:
    """A task title for a prompt: quoted and ASCII-escaped, so it can never pass as a label."""
    if not title:
        return None
    return json.dumps(strip_invisible(title)[:40], ensure_ascii=True)


# --- the broker ----------------------------------------------------------------------


class ApprovalBroker:
    """Receives permission requests on a Unix socket and decides them."""

    def __init__(
        self,
        workdir: Path,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        channel: ApprovalChannel | None = None,
        clock: Callable[[], float] = time.monotonic,
        bind_wait_s: float = BIND_WAIT_S,
        main_label: str = "chat",
    ) -> None:
        if main_label not in MAIN_LABELS:
            raise ValueError("main_label must be 'chat' or 'telegram'")
        self.workdir = Path(workdir)
        self.timeout_s = timeout_s
        self.channel = channel  # replaceable at runtime
        self.clock = clock
        self.bind_wait_s = bind_wait_s
        self.closing = threading.Event()
        self.on_delegate: Callable[[str, str | None], str] | None = None  # TaskService.delegate
        self._cond = threading.Condition()
        self._queue = _PromptQueue(self._cond)
        self._main = ApprovalScope(
            name="main", kind="main", label=main_label, token="", broker=self,
            write_deny_parts=(TASK_RESULT_PARTS,),
        )
        # The Telegram assistant in `serve` never delegates, whatever else is configured.
        self.delegation_allowed = main_label == "chat"
        self._scopes: dict[str, ApprovalScope] = {"main": self._main}
        self._waiting = 0  # requests of all scopes waiting for the prompt
        self._inflight = 0
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self._dir: Path | None = None
        self._runtime_files: list[str] = []

    # --- the main scope: the API the chat loop has always used ---

    @property
    def main(self) -> ApprovalScope:
        return self._main

    @property
    def token(self) -> str | None:
        return self._main.token or None

    @token.setter
    def token(self, value: str | None) -> None:
        self._main.token = value or ""

    @property
    def turn_active(self) -> threading.Event:
        return self._main.turn_active

    @property
    def allowed_skills(self) -> frozenset[str]:
        return self._main.allowed_skills

    @allowed_skills.setter
    def allowed_skills(self, value: frozenset[str]) -> None:
        self._main.allowed_skills = frozenset(value)

    def enable_delegation(self) -> None:
        """Offer delegate_task to the main session.

        Before start() or after it; afterwards the main MCP config is rewritten
        atomically, so call it before the main session starts.
        """
        if not self.delegation_allowed:
            raise RuntimeError("delegation is not available for this assistant")
        self._main.delegation = True
        if self._server is not None and self._dir is not None:
            self._write_main_config()

    def _write_main_config(self) -> None:
        """Write (or atomically replace) mcp.json for the main scope."""
        assert self._dir is not None
        config = build_mcp_config(self._dir / "s", self._main.token, self.timeout_s, delegate=self._main.delegation)
        target = self._dir / "mcp.json"
        if not target.exists():
            write_private(target, json.dumps(config))
        else:
            tmp = self._dir / "mcp.json.tmp"
            try:
                write_private(tmp, json.dumps(config))
                os.replace(tmp, target)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    tmp.unlink()
        self._main.mcp_config_path = target

    def begin_turn(self) -> None:
        self._begin_turn(self._main)

    def end_turn(self) -> None:
        self._end_turn(self._main)

    def set_turn_context(self, note: str) -> None:
        """Mark the active main turn, e.g. because note excerpts were attached. Reset by begin_turn."""
        self._set_turn_context(self._main, note)

    def observe(self, event: object) -> None:
        """Remember the tool calls the main session announces in the stream."""
        self._observe(self._main, event)

    # --- turn bookkeeping per scope ---

    def _begin_turn(self, scope: ApprovalScope) -> None:
        with self._cond:
            scope._tool_uses.clear()
            scope._used.clear()
            scope._refusals = 0
            scope._turn_context = None
            scope._approved_delegations.clear()
            scope._delegations = 0
            scope.turn_active.set()
            self._cond.notify_all()

    def _end_turn(self, scope: ApprovalScope) -> None:
        with self._cond:
            scope.turn_active.clear()
            scope._tool_uses.clear()
            scope._approved_delegations.clear()
            scope._delegations = 0
            self._cond.notify_all()

    def _set_turn_context(self, scope: ApprovalScope, note: str) -> None:
        note = clean_text(note, 60)
        with self._cond:
            current = scope._turn_context
            if current and note and note in current:
                return
            if current and note:
                note = f"{current}; {note}"
            scope._turn_context = clean_text(note, 120) or current

    def _observe(self, scope: ApprovalScope, event: object) -> None:
        if not isinstance(event, dict) or event.get("type") != "assistant":
            return
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return
        reads: list[tuple[str, dict]] = []
        with self._cond:
            if not scope.turn_active.is_set():
                return
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                tool_use_id, name, tool_input = block.get("id"), block.get("name"), block.get("input")
                if not (isinstance(tool_use_id, str) and tool_use_id and isinstance(name, str)
                        and isinstance(tool_input, dict)):
                    continue
                if scope.kind == "main" and name in READ_TOOLS:
                    reads.append((name, tool_input))
                if tool_use_id in scope._tool_uses:
                    continue  # the first announcement wins
                scope._tool_uses[tool_use_id] = _Announced(name, tool_input, input_digest(tool_input))
                while len(scope._tool_uses) > MAX_TOOL_USES:
                    scope._tool_uses.popitem(last=False)
            self._cond.notify_all()
        # Claude Code runs Read/Glob/Grep inside the vault without asking the permission
        # tool, so the stream is where a read of helper-written data shows. Mark the turn
        # here; the same check in _decide only covers reads that do reach the broker.
        if any(reads_helper_data(name, tool_input, self.workdir) for name, tool_input in reads):
            self._set_turn_context(scope, HELPER_OUTPUT_CONTEXT)

    # --- scopes ---

    def _live(self, scope: ApprovalScope) -> bool:
        return self._scopes.get(scope.name) is scope and scope.turn_active.is_set()

    def _scope_for_token(self, token: str) -> ApprovalScope | None:
        """The scope whose token matches. Every token is compared (no early exit)."""
        with self._cond:
            scopes = list(self._scopes.values())
        match = None
        for scope in scopes:
            if scope.token and hmac.compare_digest(token.encode("utf-8"), scope.token.encode("utf-8")):
                match = scope
        return match

    def open_scope(
        self,
        name: str,
        *,
        kind: str = "helper",
        task_id: str | None = None,
        title: str | None = None,
        domain: str | None = None,
        delegated: bool = False,
        telegram: bool = False,
    ) -> ApprovalScope:
        """A new helper scope with its own token and MCP config file (mcp-<name>.json)."""
        if self._dir is None or self._server is None or self.closing.is_set():
            raise RuntimeError("the approval service is not running")
        if kind != "helper":
            raise RuntimeError("only helper scopes can be opened")
        if not SCOPE_NAME_RE.fullmatch(name) or name == "main":
            raise RuntimeError("invalid scope name")
        if task_id is not None and not TASK_ID_RE.fullmatch(task_id):
            raise RuntimeError("invalid task id")
        if domain is not None and not valid_domain(domain):
            raise RuntimeError("invalid domain")
        label = f"task {task_id}" if task_id else name
        if domain:
            label += f" ({domain})"
        if delegated:
            label += " · delegated"
        if telegram:
            label += " · telegram"  # queued from Telegram: the person sees it in every prompt
        scope = ApprovalScope(
            name=name, kind="helper", label=label, token=secrets.token_urlsafe(32),
            origin=_quoted_title(title), task_id=task_id,
            domain_file=f"wiki/domains/{domain}.md" if domain else None, broker=self,
        )
        with self._cond:
            if name in self._scopes:
                raise RuntimeError("scope already open")
            if len(self._scopes) >= MAX_SCOPES:
                raise RuntimeError("too many approval scopes")
            self._scopes[name] = scope
        try:
            config = build_mcp_config(self._dir / "s", scope.token, self.timeout_s)
            scope.mcp_config_path = self.write_runtime_file(f"mcp-{name}.json", json.dumps(config))
        except BaseException:
            with self._cond:
                self._scopes.pop(name, None)
            raise
        return scope

    def close_scope(self, scope: ApprovalScope) -> None:
        """End the scope's turn, refuse its token from now on and remove its MCP config. Idempotent."""
        if scope is self._main:
            raise RuntimeError("the main scope closes with the broker")
        self._end_turn(scope)
        with self._cond:
            if self._scopes.get(scope.name) is scope:
                del self._scopes[scope.name]
            self._cond.notify_all()
        if scope.mcp_config_path is not None:
            self._remove_runtime_file(scope.mcp_config_path.name)

    def scopes(self) -> list[str]:
        with self._cond:
            return list(self._scopes)

    def scope_pending(self, scope: ApprovalScope) -> bool:
        """Whether a decision of this scope waits for or holds the prompt."""
        with self._cond:
            return scope._waiting > 0 or scope._prompting

    def request_preempted(self, request: ApprovalRequest) -> bool:
        """Whether a helper's prompt ends because the main session waits for the prompt."""
        if self.closing.is_set():
            return False
        with self._cond:
            scope = self._scopes.get(request.scope)
            if scope is None or scope.kind == "main" or not scope.turn_active.is_set():
                return False
            return self._queue.main_waiting()

    def request_stopped(self, request: ApprovalRequest) -> bool:
        """Whether a prompt on screen must end now (it then counts as a deny, not a refusal).

        True when the broker closes, the request's scope is gone or its turn ended, or
        a helper's prompt is in the way of a waiting main-session request.
        """
        if self.closing.is_set():
            return True
        with self._cond:
            scope = self._scopes.get(request.scope)
            if scope is None or not scope.turn_active.is_set():
                return True
            return scope.kind != "main" and self._queue.main_waiting()

    # --- decision ---

    def decide(
        self,
        tool_name: str,
        tool_input: object,
        tool_use_id: str | None = None,
        input_sha256: str | None = None,
        scope: ApprovalScope | None = None,
    ) -> Verdict:
        scope = scope or self._main
        request_id = secrets.token_urlsafe(6)
        level: Level | None = None
        with self._cond:
            self._inflight += 1
        try:
            verdict, level, request_id = self._decide(scope, tool_name, tool_input, tool_use_id, input_sha256)
        except Exception as exc:  # noqa: BLE001 - every failure is a deny
            log.warning("approval failed: %s", type(exc).__name__)
            verdict = Verdict(False, "approval failed")
        finally:
            with self._cond:
                self._inflight -= 1
                self._cond.notify_all()
        log.info(
            "approval %s %s %s level %s -> %s",
            request_id,
            scope.name,
            clean_text(tool_name, 80),
            int(level) if level is not None else "-",
            "allow" if verdict.allow else "deny",
        )
        return verdict

    def _match(self, scope: ApprovalScope, tool_name: str, tool_input: dict, tool_use_id: str | None,
               digest: str) -> tuple[dict | None, str]:
        """The announced input of this tool call in this scope, or the reason there is none."""
        if not tool_use_id:
            return None, "no matching tool call"
        end = time.monotonic() + self.bind_wait_s
        with self._cond:
            while True:
                if not scope.turn_active.is_set():
                    return None, "no active turn"
                entry = scope._tool_uses.get(tool_use_id)
                if entry is not None:
                    break
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return None, "no matching tool call"
                self._cond.wait(remaining)
            if tool_use_id in scope._used:
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
            scope._used.add(tool_use_id)
            return entry.input, ""

    def _refuse(self, scope: ApprovalScope, message: str) -> Verdict:
        with self._cond:
            scope._refusals += 1
        return Verdict(False, message)

    def _helper_write(self, scope: ApprovalScope, tool_name: str, announced: dict) -> tuple[str | None, str | None]:
        """(why a helper write is denied without asking, the summary for an allowed append).

        An allowlist: the only write a helper may ask for is an Edit that appends notes to
        its own wiki/domains/<domain>.md.
        """
        if writes_under(tool_name, announced, self.workdir, ("outputs",)):
            return "helpers may not write outputs/", None
        if tool_name != "Edit" or scope.domain_file is None:
            return "helpers may only append notes to their own domain file", None
        values = list(path_values(tool_name, announced))
        expected = tuple(scope.domain_file.split("/"))
        file_path = announced.get("file_path")
        if len(values) != 1 or not isinstance(file_path, str):
            return "helpers may only append notes to their own domain file", None
        try:
            parts = _relative_parts(file_path, self.workdir)
        except (OSError, ValueError, RuntimeError):
            return "invalid path", None
        if parts != expected:
            return "helpers may only append notes to their own domain file", None
        problem, appended = domain_append_problem(announced, scope.task_id)
        if problem:
            return problem, None
        # The summary shows only the appended lines, so the position must be checked
        # against the file itself: an old_string in the middle (the Links line, the
        # Notes heading, half of an earlier note) would otherwise insert text there.
        content = Vault(self.workdir).read_text(scope.domain_file, MAX_DOMAIN_FILE_BYTES)
        problem = domain_append_location_problem(content, announced["old_string"])
        if problem:
            return problem, None
        summary =(f"file_path={json.dumps(scope.domain_file, ensure_ascii=True)}, "
                   f"append={json.dumps(appended, ensure_ascii=True)}")
        if len(summary) > MAX_APPEND_SUMMARY:
            return "this domain note is too long to review", None
        return None, summary

    def _decide(
        self, scope: ApprovalScope, tool_name: str, tool_input: object, tool_use_id: str | None,
        digest: str | None,
    ) -> tuple[Verdict, Level | None, str]:
        rid = secrets.token_urlsafe(6)
        if tool_name == PERMISSION_TOOL:
            return Verdict(False, "the approval tool cannot be used directly"), None, rid
        if self.closing.is_set():
            return Verdict(False, "the approval service is closing"), None, rid
        if not scope.turn_active.is_set():
            return Verdict(False, "no active turn"), None, rid
        if not isinstance(tool_name, str) or not isinstance(tool_input, dict):
            return Verdict(False, "invalid request"), None, rid
        timeout = self.timeout_s if scope.kind == "main" else min(self.timeout_s, HELPER_PROMPT_TIMEOUT_S)
        deadline = self.clock() + timeout
        if digest is None:
            digest = input_digest(tool_input)
        announced, reason = self._match(scope, tool_name, tool_input, tool_use_id, digest)
        if announced is None:
            return Verdict(False, reason), None, rid
        reason = path_problem(tool_name, announced, self.workdir)
        if reason:
            return Verdict(False, reason), None, rid
        if tool_name == DELEGATE_TOOL:
            return self._decide_delegate(scope, announced, tool_use_id, deadline, rid)
        append_summary = None
        if tool_name in WRITE_TOOLS:
            if scope.kind != "main":
                problem, append_summary = self._helper_write(scope, tool_name, announced)
                if problem:
                    return Verdict(False, problem), None, rid
            for prefix in scope.write_deny_parts:
                if writes_under(tool_name, announced, self.workdir, prefix):
                    return Verdict(False, "task results are written by the agent only"), None, rid
        if tool_name == SKILL_TOOL:
            # The bridge routes every Skill call here (permissions "ask"). Only the
            # skills of the checked, loaded plugins are allowed, without a prompt;
            # built-in skills (update-config, schedule, ...) are denied without asking.
            # Helpers have no allowed skills.
            skill = announced.get("skill")
            if isinstance(skill, str) and skill.lstrip("/") in scope.allowed_skills:
                self._set_turn_context(scope, SKILL_CONTEXT)
                return Verdict(True, "allowed plugin skill"), Level.READ, rid
            return Verdict(False, "this skill is not allowed"), None, rid
        # classify() itself stays fail-safe: any other route of Skill here is level 3.
        level = classify(tool_name, announced, self.workdir)
        if level is Level.READ:
            if scope.kind == "main" and reads_helper_data(tool_name, announced, self.workdir):
                # Injected text in task results or domain notes could steer later requests.
                # Backup only: such reads are normally marked from the stream (_observe).
                self._set_turn_context(scope, HELPER_OUTPUT_CONTEXT)
            return Verdict(True, "read-only"), level, rid
        if is_protected(tool_name, announced, self.workdir):
            return Verdict(False, "protected path: the assistant may not change it"), level, rid

        if append_summary is not None:
            summary, was_cut = append_summary, False
        else:
            summary, was_cut = _render(tool_name, announced, 400)
        request = self._request(scope, tool_name, level, summary, tool_use_id,
                                render_full(announced) if level >= Level.DANGER and was_cut else None)
        return self._ask(scope, request, deadline), level, request.request_id

    def _request(self, scope: ApprovalScope, tool_name: str, level: Level, summary: str,
                 tool_use_id: str | None, full: str | None) -> ApprovalRequest:
        with self._cond:
            context = scope._turn_context
        return ApprovalRequest(
            tool_name=display_name(tool_name),
            level=level,
            summary=summary,
            tool_use_id=tool_use_id,
            full=full,
            context=context,
            scope=scope.name,
            scope_label=scope.label,
            origin=scope.origin,
        )

    def _ask(self, scope: ApprovalScope, request: ApprovalRequest, deadline: float) -> Verdict:
        """Queue for the prompt and ask the person through the channel."""
        helper = scope.kind != "main"
        with self._cond:
            if scope._refusals >= MAX_REFUSALS_PER_TURN:
                return Verdict(False, "too many refused approvals in this turn")
            if helper and scope._prompts_shown >= MAX_PROMPTS_PER_HELPER_TASK:
                return Verdict(False, "helper used its approval budget")
            if scope._waiting >= MAX_WAITING or self._waiting >= MAX_WAITING_TOTAL:
                return Verdict(False, "too many pending approvals")
            scope._waiting += 1
            self._waiting += 1
        try:
            acquired = self._queue.acquire(1 if helper else 0, deadline, self.clock, self.closing,
                                           lambda: not self._live(scope))
        finally:
            with self._cond:
                scope._waiting -= 1
                self._waiting -= 1
        if not acquired:
            if self.closing.is_set() or not self._live(scope):
                return Verdict(False, "no active turn")
            return Verdict(False, "approval timed out")
        try:
            with self._cond:
                if scope._refusals >= MAX_REFUSALS_PER_TURN:
                    return Verdict(False, "too many refused approvals in this turn")
                if helper and scope._prompts_shown >= MAX_PROMPTS_PER_HELPER_TASK:
                    return Verdict(False, "helper used its approval budget")
                scope._prompting = True
                if helper:
                    scope._prompts_shown += 1
            if self.closing.is_set() or not self._live(scope):
                return Verdict(False, "no active turn")
            channel = self.channel
            if channel is None:
                return Verdict(False, "no approval channel is available")
            try:
                approved = channel.ask(request, deadline) is True
            except Exception as exc:  # noqa: BLE001
                log.warning("approval channel failed: %s", type(exc).__name__)
                approved = False
            if self.clock() > deadline:
                return self._refuse(scope, "approval timed out")
            if self.closing.is_set() or not self._live(scope):
                return Verdict(False, "no active turn")
            if not approved:
                if helper and self._queue.main_waiting():
                    # Withdrawn for the chat's own request: not the person's refusal, and
                    # not a prompt of the helper's budget either.
                    with self._cond:
                        scope._prompts_shown = max(0, scope._prompts_shown - 1)
                    return Verdict(False, "the chat needed the approval prompt first; the request was withdrawn")
                return self._refuse(scope, "the person did not approve this action")
            return Verdict(True, "approved by the person")
        finally:
            with self._cond:
                scope._prompting = False
            self._queue.release()

    # --- delegation (main session only) ---

    def _decide_delegate(self, scope: ApprovalScope, announced: dict, tool_use_id: str | None,
                         deadline: float, rid: str) -> tuple[Verdict, Level | None, str]:
        """The permission request for delegate_task: deny without asking, or ask the person."""
        if (scope.kind != "main" or not scope.delegation or self.on_delegate is None
                or not self.delegation_allowed):
            # Without a task service the call could never run: never ask the person for it.
            return Verdict(False, "delegation is off"), None, rid
        with self._cond:
            if scope._delegations >= MAX_DELEGATIONS_PER_TURN:
                return Verdict(False, "one delegation per turn"), None, rid
            context = scope._turn_context
        problem = delegate_input_problem(announced)
        if problem:
            return Verdict(False, problem), None, rid
        # From possibly injected text (notes, a skill), unattended work needs the typed confirmation.
        level = Level.DANGER if context else Level.WRITE
        summary = f"queue a background task: text={json.dumps(announced['text'], ensure_ascii=True)}"
        if "domain" in announced:
            summary += f", domain={json.dumps(announced['domain'], ensure_ascii=True)}"
        request = self._request(scope, DELEGATE_TOOL, level, summary, tool_use_id, None)
        verdict = self._ask(scope, request, deadline)
        if verdict.allow:
            with self._cond:
                if not scope.turn_active.is_set():
                    return Verdict(False, "no active turn"), level, request.request_id
                scope._approved_delegations.append(input_digest(announced))
                scope._delegations += 1
        return verdict, level, request.request_id

    def delegate(self, scope: ApprovalScope, frame: dict) -> Verdict:
        """The delegate_task call itself: only with a one-shot approval of exactly this input."""
        tool_input, sha = frame.get("input"), frame.get("input_sha256")
        verdict = self._delegate(scope, tool_input, sha)
        log.info("delegation %s -> %s", scope.name, "queued" if verdict.allow else "refused")
        return verdict

    def _delegate(self, scope: ApprovalScope, tool_input: object, sha: object) -> Verdict:
        if not isinstance(tool_input, dict) or not isinstance(sha, str):
            return Verdict(False, "invalid request")
        if (scope.kind != "main" or not scope.delegation or self.on_delegate is None
                or not self.delegation_allowed):
            return Verdict(False, "delegation is off")
        if self.closing.is_set() or not self._live(scope):
            return Verdict(False, "no active turn")
        digest = input_digest(tool_input)
        with self._cond:
            if not hmac.compare_digest(digest, sha) or digest not in scope._approved_delegations:
                return Verdict(False, "this delegation was not approved")
            scope._approved_delegations.remove(digest)  # one-shot
        problem = delegate_input_problem(tool_input)
        if problem:
            return Verdict(False, problem)
        domain = tool_input.get("domain")
        try:
            task_id = self.on_delegate(tool_input["text"], domain if isinstance(domain, str) else None)
        except RuntimeError as exc:  # TaskError, VaultError
            return Verdict(False, clean_text(str(exc), 200) or "delegation failed")
        except Exception as exc:  # noqa: BLE001
            log.warning("delegation failed: %s", type(exc).__name__)
            return Verdict(False, "delegation failed")
        if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
            return Verdict(False, "delegation failed")
        return Verdict(True, "queued", task_id=task_id)

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
        """Listen on a private Unix socket and write the main MCP config. Returns its path."""
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
            self._main.token = secrets.token_urlsafe(32)
            sock_path = directory / "s"
            server = _Server(str(sock_path), _Handler)
            server.broker = self  # type: ignore[attr-defined]
            os.chmod(sock_path, 0o600)
            self._server = server
            self._write_main_config()
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

    def _remove_runtime_file(self, name: str) -> None:
        directory = self._dir
        if directory is None or name not in self._runtime_files:
            return
        try:
            (directory / name).unlink()
        except OSError:
            pass
        self._runtime_files.remove(name)

    def close(self, join_s: float = CLOSE_JOIN_S) -> None:
        """Stop listening and remove the socket, the MCP configs and the directory.

        Running decisions see `closing` and stop; they get up to join_s to finish,
        so a channel can still clean up (e.g. cancel a pending remote approval).
        """
        self.closing.set()
        with self._cond:
            scopes = list(self._scopes.values())
        for scope in scopes:
            self._end_turn(scope)
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
