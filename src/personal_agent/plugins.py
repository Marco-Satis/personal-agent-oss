"""Plugins: standard Claude Code plugin folders, checked before they are loaded.

A plugin is a folder with `.claude-plugin/plugin.json` plus `skills/`,
`commands/` and `agents/` Markdown files. Enabled plugins are copied (never
symlinked) to `state_dir/plugins/<name>/` and registered with a digest in
`state_dir/plugins.json`; the shipped core plugin lives in the repository at
`plugins/core`. Every chat start checks every plugin again and passes each one
with its own `--plugin-dir`.

Why the checker refuses so much (spike with Claude Code 2.1.302):

- Plugin hooks run arbitrary shell commands even under `--restricted` and
  `--strict-mcp-config`, with the session environment (including an OAuth token
  when one is configured) and the Claude Code config dir. No checker rule could
  make that safe, so any plugin with hooks is refused, without an override.
- Plugin MCP servers start as soon as `--strict-mcp-config` is missing, so any
  plugin with `.mcp.json`, `.lsp.json` or server keys is refused as well.
- The plugin folder lies outside the vault, so the assistant's file tools cannot
  read supporting files: a skill is exactly one self-contained SKILL.md.
- Skill and command text expands into the prompt: shell-injection syntax,
  `@file` references and `${...}` substitutions are refused, as are network
  calls, credentials, obfuscated blobs and invisible characters.

The bridge adds defense in depth (`disableAllHooks`, the Skill permission rule,
`--strict-mcp-config`) and the router a stream tripwire; see router.guarded_send.

The checker is pure and read-only: it never imports or runs anything from the
plugin, walks directory descriptors without following symlinks, and opens files
non-blocking and checks them with fstat, so a FIFO swapped in cannot hang it.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import stat
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import personal_agent

from .textsafe import is_invisible

if TYPE_CHECKING:
    from .config import Config

log = logging.getLogger(__name__)

MAX_MANIFEST_BYTES = 16 * 1024
MAX_FILE_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 1024 * 1024
MAX_FILES = 200
MAX_DEPTH = 5
MAX_NAME_LEN = 100
MAX_PLUGIN_NAME = 64
MAX_COMPONENTS = 20  # skills + commands + agents per plugin
MAX_DESCRIPTION_CHARS = 1024
MAX_DESCRIPTION_BYTES = 4 * 1024  # all descriptions of all loaded plugins (always in the model context)
MAX_REGISTRY_BYTES = 256 * 1024
MAX_FINDINGS = 200
MAX_FINDINGS_PER_RULE = 5  # per file
STALE_LEFTOVER_S = 3600
CORE_NAME = "core"
RESERVED_PREFIXES = ("cc-",)  # built-in plugins are named cc-plugin-*
PLUGINS_DIR = "plugins"
REGISTRY_FILE = "plugins.json"
MANIFEST = ".claude-plugin/plugin.json"
REGISTRY_VERSION = 1

NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")  # use fullmatch; at most MAX_PLUGIN_NAME characters
_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_VERSION = re.compile(r"[0-9A-Za-z.+-]{1,32}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_FM_KEY = re.compile(r"[a-z][a-z0-9-]*")
_YAML_NON_STRING = re.compile(r"(true|false|yes|no|on|off|y|n|null|~|[0-9][0-9._+-]*)", re.I)

TOP_DIRS = frozenset({".claude-plugin", "skills", "agents", "commands"})
DOC_FILES = frozenset({"README.md", "LICENSE", "LICENSE.md", "LICENSE.txt", "CHANGELOG.md"})
TOP_HIDDEN = frozenset({".claude-plugin", ".mcp.json", ".lsp.json"})
HOOKS_MESSAGE = "hooks execute commands even under --restricted (refused)"
MCP_MESSAGE = "plugin MCP servers execute code (refused)"
LSP_MESSAGE = "plugin language servers execute code (refused)"
NAMED_ENTRIES = {
    "hooks": ("hooks", HOOKS_MESSAGE),
    "hooks.json": ("hooks", HOOKS_MESSAGE),
    ".mcp.json": ("mcp", MCP_MESSAGE),
    ".lsp.json": ("lsp", LSP_MESSAGE),
    "bin": ("executable-dir", "executables are not allowed"),
    "scripts": ("executable-dir", "scripts are not allowed"),
}

MANIFEST_KEYS = frozenset({"name", "version", "description", "author", "homepage", "repository", "license", "keywords"})
MANIFEST_NAMED = {
    "hooks": ("hooks", HOOKS_MESSAGE),
    "mcpServers": ("mcp", MCP_MESSAGE),
    "lspServers": ("lsp", LSP_MESSAGE),
}
FRONTMATTER_KEYS = {
    "skill": frozenset({"name", "description", "license", "argument-hint", "disable-model-invocation", "user-invocable"}),
    "command": frozenset({"description", "argument-hint"}),
    "agent": frozenset({"name", "description", "color"}),
}
FRONTMATTER_BOOLS = frozenset({"disable-model-invocation", "user-invocable"})
FRONTMATTER_NAMED = {
    "hooks": ("hooks", HOOKS_MESSAGE),
    "allowed-tools": ("allowed-tools", "pre-approved tools would bypass the approval broker"),
    "allowedTools": ("allowed-tools", "pre-approved tools would bypass the approval broker"),
    "model": ("model", "plugins may not choose the model (quota)"),
    "tools": ("manifest-key", "plugins may not choose tools"),
    "mcpServers": ("manifest-key", "plugins may not declare MCP servers"),
    "permissionMode": ("manifest-key", "plugins may not change the permission mode"),
}
VALUE_STARTERS = tuple("[{|>&*!%@`")

_MAGIC = (b"\x7fELF", b"MZ", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"PK\x03\x04")
_SUSPECT = re.compile(r"[^\x20-\x7e\t]")


def _patterns(*sources: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(source, re.IGNORECASE) for source in sources)


# All scans run on NFKC-normalized, casefolded lines with IGNORECASE, so full-width
# and upper-case variants ("ｈｔｔｐｓ://", "CURL", "WEBFETCH") are caught too.
CREDENTIAL_PATTERNS = _patterns(
    r"sk-ant-[a-z0-9_-]{10,}",
    r"\bsk-(proj-)?[a-z0-9]{20,}",
    r"gh[pousr]_[a-z0-9]{30,}",
    r"github_pat_[a-z0-9_]{20,}",
    r"akia[0-9a-z]{16}",
    r"xox[abprs]-[a-z0-9-]{10,}",
    r"aiza[0-9a-z_-]{35}",
    r"-----begin [a-z ]*private key-----",
    r"discord(app)?\.com/api/webhooks/",
    r"(api[_-]?key|secret|token|password|passwd)\s*[:=]\s*[\"']?[a-z0-9_./+=-]{16,}",
)
NETWORK_PATTERNS = _patterns(
    r"https?://",
    r"\bwww\.",
    r"\b(curl|wget|nc|ncat|telnet|ssh|scp|rsync)\b",
    r"fetch\(",
    r"xmlhttprequest",
    r"\bwebfetch\b",
    r"\bwebsearch\b",
    r"\b\d{1,3}(\.\d{1,3}){3}\b",
)
OBFUSCATION_PATTERNS = _patterns(
    r"[a-z0-9+/]{120,}={0,2}",
    r"[0-9a-f]{80,}",
    r"(\\x[0-9a-f]{2}){8,}",
    r"\b(eval|exec)\s*\(",
    r"atob\(",
    r"fromcharcode",
    r"base64\s+(-d|--decode)",
    r"!`",              # shell injection into skill/command text
    r"(```|~~~)\s*!",   # fenced exec blocks
    r"\$\(",
    r"\$\{",            # substitutions such as ${CLAUDE_PLUGIN_ROOT}; plain $ARGUMENTS is fine
)
# Any "@" that starts a word, quoted forms such as @"~/x" and @'../x' included: the
# CLI's mention syntax accepts quoted paths, so only a bare "@" stays allowed.
FILE_REFERENCE = re.compile(r"(^|[\s(\[{\"'`])@\S", re.IGNORECASE)
INJECTION_PATTERNS = _patterns(
    r"ignore (all )?(previous|prior) instructions",
    r"disregard the system prompt",
)

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


class PluginError(RuntimeError):
    pass


@dataclass(frozen=True)
class Finding:
    severity: str  # "error" | "warning"
    path: str  # plugin-relative POSIX path, "." for the root
    line: int | None
    rule: str  # e.g. "hooks", "mcp", "symlink", "credential", "network", "obfuscation"
    message: str  # never contains the matched text


@dataclass(frozen=True)
class Component:
    kind: str  # "skill" | "command" | "agent"
    name: str
    description: str


@dataclass(frozen=True)
class CheckReport:
    root: Path
    name: str | None
    version: str | None
    findings: tuple[Finding, ...]
    files: int
    total_bytes: int
    digest: str | None
    components: tuple[Component, ...] = ()

    @property
    def ok(self) -> bool:
        return not any(f.severity == "error" for f in self.findings)

    @property
    def description_bytes(self) -> int:
        return sum(len(c.description.encode("utf-8")) for c in self.components)

    @property
    def invocable(self) -> tuple[str, ...]:
        """"<plugin>:<name>" of every skill and command (Skill tool and "/" messages)."""
        if not self.name:
            return ()
        return tuple(f"{self.name}:{c.name}" for c in self.components if c.kind in ("skill", "command"))


@dataclass(frozen=True)
class InstalledPlugin:
    name: str
    path: Path
    version: str | None
    status: str  # "ok" | "changed" | "invalid" | "unregistered"
    report: CheckReport
    shipped: bool = False


@dataclass(frozen=True)
class LoadedPlugin:
    name: str
    path: Path
    report: CheckReport


def is_reserved(name: str) -> bool:
    return name == CORE_NAME or name.startswith(RESERVED_PREFIXES)


def valid_name(name: object) -> bool:
    return isinstance(name, str) and len(name) <= MAX_PLUGIN_NAME and NAME_RE.fullmatch(name) is not None


def shown(text: object) -> str:
    """Safe for a terminal and a log: plain when harmless, else ASCII-escaped JSON."""
    text = str(text)
    if re.fullmatch(r"[A-Za-z0-9 ._/:+~=@,()-]{0,200}", text):
        return text
    return json.dumps(text[:200], ensure_ascii=True)


# --- findings -----------------------------------------------------------------------


class _Findings:
    def __init__(self) -> None:
        self.items: list[Finding] = []
        self.per_rule: dict[tuple[str, str], int] = {}
        self.structural = False  # a file could not be read as checked: no digest
        self.omitted = 0
        self.full = False

    def add(self, severity: str, path: str, line: int | None, rule: str, message: str) -> None:
        key = (path, rule, severity)
        count = self.per_rule.get(key, 0)
        self.per_rule[key] = count + 1
        if count >= MAX_FINDINGS_PER_RULE:
            self.omitted += 1  # the same rule already reported this file with this severity
            return
        if len(self.items) >= MAX_FINDINGS:
            self.omitted += 1
            if severity == "error" and not self.full:
                self.full = True  # keep the verdict right even when the list is full
                self.items.append(Finding("error", ".", None, "omitted", "further findings omitted"))
            return
        self.items.append(Finding(severity, path or ".", line, rule, message))

    def error(self, path: str, line: int | None, rule: str, message: str) -> None:
        self.add("error", path, line, rule, message)

    def warning(self, path: str, line: int | None, rule: str, message: str) -> None:
        self.add("warning", path, line, rule, message)


class _Stop(Exception):
    """A cap was reached; the walk ends."""


# --- reading ------------------------------------------------------------------------


class _Walk:
    def __init__(self, findings: _Findings) -> None:
        self.findings = findings
        self.files: dict[str, bytes] = {}
        self.dirs: list[str] = []
        self.count = 0
        self.total = 0

    def run(self, fd: int) -> None:
        try:
            self._dir(fd, "", 0)
        except _Stop:
            self.findings.structural = True

    def _structural(self, path: str, rule: str, message: str) -> None:
        self.findings.structural = True
        self.findings.error(path, None, rule, message)

    def _dir(self, fd: int, rel: str, depth: int) -> None:
        try:
            with os.scandir(fd) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            self._structural(rel, "unreadable", f"cannot list the folder ({type(exc).__name__})")
            return
        for entry in entries:
            name = entry.name
            path = f"{rel}/{name}" if rel else name
            if rel == "" and name in TOP_HIDDEN:
                pass  # .claude-plugin is allowed; .mcp.json/.lsp.json get their own rule in the layout check
            elif len(name) > MAX_NAME_LEN or not _COMPONENT.fullmatch(name):
                if name.startswith(".") and _COMPONENT.fullmatch(name[1:] or "-") and len(name) <= MAX_NAME_LEN:
                    self._structural(path, "hidden", "hidden entries are not allowed")
                else:
                    self._structural(path, "filename",
                                     "names may only use A-Z a-z 0-9 . _ - and start with a letter or digit")
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                self._structural(path, "unreadable", f"cannot read the entry ({type(exc).__name__})")
                continue
            if stat.S_ISLNK(info.st_mode):
                self._structural(path, "symlink", "symbolic links are not allowed")
            elif stat.S_ISDIR(info.st_mode):
                if depth + 1 > MAX_DEPTH:
                    self._structural(path, "depth", f"folders nested deeper than {MAX_DEPTH} levels")
                    raise _Stop
                self.dirs.append(path)
                try:
                    child = os.open(name, _DIR_FLAGS, dir_fd=fd)
                except OSError as exc:
                    self._structural(path, "unreadable", f"cannot open the folder ({type(exc).__name__})")
                    continue
                try:
                    self._dir(child, path, depth + 1)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                self._file(fd, name, path)
            else:
                self._structural(path, "special-file", "not a regular file or folder (fifo, socket or device)")

    def _file(self, fd: int, name: str, path: str) -> None:
        self.count += 1
        if self.count > MAX_FILES:
            self._structural(path, "too-many-files", f"more than {MAX_FILES} files")
            raise _Stop
        try:
            handle = os.open(name, _READ_FLAGS, dir_fd=fd)
        except OSError as exc:
            rule = "symlink" if exc.errno == errno.ELOOP else "unreadable"
            self._structural(path, rule, f"cannot open the file ({type(exc).__name__})")
            return
        try:
            info = os.fstat(handle)
            if not stat.S_ISREG(info.st_mode):
                self._structural(path, "special-file", "not a regular file")
                return
            if info.st_nlink > 1:
                self._structural(path, "hardlink", "hard links are not allowed (the file may alias one outside)")
                return
            if info.st_mode & 0o111:
                self._structural(path, "executable", "files may not be executable")
                return
            if info.st_size > MAX_FILE_BYTES:
                self._structural(path, "file-size", f"file larger than {MAX_FILE_BYTES // 1024} KiB")
                raise _Stop
            data = _read_capped(handle, MAX_FILE_BYTES)
        except OSError as exc:
            self._structural(path, "unreadable", f"cannot read the file ({type(exc).__name__})")
            return
        finally:
            os.close(handle)
        if data is None:
            self._structural(path, "file-size", f"file larger than {MAX_FILE_BYTES // 1024} KiB")
            raise _Stop
        self.total += len(data)
        if self.total > MAX_TOTAL_BYTES:
            self._structural(path, "total-size", f"plugin larger than {MAX_TOTAL_BYTES // 1024} KiB in total")
            raise _Stop
        self.files[path] = data


def _read_capped(fd: int, cap: int) -> bytes | None:
    chunks: list[bytes] = []
    total = 0
    while total <= cap:
        chunk = os.read(fd, min(65536, cap + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return None if total > cap else b"".join(chunks)


# --- layout ---------------------------------------------------------------------------


def _check_layout(walk: _Walk, findings: _Findings) -> None:
    dirs = set(walk.dirs)
    refused: list[str] = []
    for path in sorted(dirs | set(walk.files)):
        if any(path.startswith(prefix + "/") for prefix in refused):
            continue
        parts = path.split("/")
        is_dir = path in dirs
        # Only top-level entries are named: skills/hooks/SKILL.md is an ordinary skill.
        named = NAMED_ENTRIES.get(parts[0]) if len(parts) == 1 else None
        if named is not None:
            findings.error(path, None, *named)
            refused.append(path)
            continue
        top = parts[0]
        if len(parts) == 1:
            if (is_dir and top in TOP_DIRS) or (not is_dir and top in DOC_FILES):
                continue
            findings.error(path, None, "unexpected-entry", "not part of a supported plugin layout")
            refused.append(path)
            continue
        if top not in TOP_DIRS:
            continue
        if top == ".claude-plugin":
            if path != MANIFEST:
                findings.error(path, None, "manifest-dir", ".claude-plugin/ may only contain plugin.json")
                refused.append(path)
        elif top == "skills":
            if len(parts) == 2:
                if not is_dir:
                    findings.error(path, None, "skill-layout", "skills/ may only contain one folder per skill")
            elif not (len(parts) == 3 and parts[2] == "SKILL.md" and not is_dir):
                findings.error(path, None, "skill-extra-file",
                               "a skill folder may only contain SKILL.md (the assistant cannot read files outside the vault)")
                refused.append(path)
        elif not (len(parts) == 2 and not is_dir and parts[1].endswith(".md")):
            findings.error(path, None, "layout", f"{top}/ may only contain .md files, no folders")
            refused.append(path)
    if MANIFEST not in walk.files:
        findings.error(MANIFEST, None, "manifest", "missing .claude-plugin/plugin.json")
    for path in sorted(dirs):
        parts = path.split("/")
        if len(parts) == 2 and parts[0] == "skills" and f"{path}/SKILL.md" not in walk.files:
            findings.error(path, None, "skill-layout", "the skill folder has no SKILL.md")


# --- content ---------------------------------------------------------------------------


def _kind(path: str) -> str:
    if path == MANIFEST:
        return "manifest"
    if path in DOC_FILES:
        return "doc"
    top = path.split("/", 1)[0]
    return {"skills": "skill", "commands": "command", "agents": "agent"}.get(top, "other")


def _decode(path: str, data: bytes, findings: _Findings) -> str | None:
    if data.startswith(_MAGIC):
        findings.error(path, None, "binary", "binary executable or archive")
        return None
    if b"\0" in data:
        findings.error(path, None, "binary", "contains NUL bytes")
        return None
    if data.startswith(b"\xef\xbb\xbf"):
        findings.error(path, 1, "bom", "starts with a byte order mark")
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        findings.error(path, None, "binary", "not valid UTF-8 text")
        return None
    if "\r" in text:
        findings.error(path, text[: text.index("\r")].count("\n") + 1, "carriage-return",
                       "carriage returns are not allowed (use \\n line endings)")
    if text.startswith("#!"):
        findings.error(path, 1, "shebang", "starts with a shebang line")
    return text


def _scan_lines(path: str, kind: str, text: str, body_start: int, findings: _Findings) -> None:
    prompt_text = kind in ("skill", "command", "agent")
    for number, line in enumerate(text.split("\n"), 1):
        if _SUSPECT.search(line):
            for ch in line:
                category = unicodedata.category(ch)
                if ch != "\r" and (is_invisible(ch) or category in ("Zl", "Zp")
                                   or (category == "Cc" and ch != "\t")):
                    findings.error(path, number, "obfuscation", "invisible, bidi or control character")
                    break
        norm = unicodedata.normalize("NFKC", line).casefold()
        if any(p.search(norm) for p in CREDENTIAL_PATTERNS):
            findings.error(path, number, "credential", "looks like a credential or webhook")
        if any(p.search(norm) for p in OBFUSCATION_PATTERNS):
            findings.error(path, number, "obfuscation", "encoded blob, eval/exec or shell-expansion syntax")
        if prompt_text and any(p.search(norm) for p in NETWORK_PATTERNS):
            findings.error(path, number, "network", "URL, network tool or web tool")
        if prompt_text and FILE_REFERENCE.search(norm):
            findings.error(path, number, "file-reference", "@file references could inline files from outside the vault")
        if number >= body_start and any(p.search(norm) for p in INJECTION_PATTERNS):
            findings.warning(path, number, "injection-phrase", "text that tries to override instructions")


def parse_frontmatter(text: str) -> tuple[list[tuple[str, str, int]], list[tuple[int, str]], int]:
    """(key, value, line) entries, (line, message) problems and the first body line.

    A deliberately small subset of YAML: "key: value" lines, continuation lines
    indented by at least two spaces, optional simple quotes. Everything a real YAML
    parser could read differently (tabs, flow or block syntax, anchors, tags, merge
    keys, comments, a document end) is a problem instead of a guess.
    """
    lines = text.split("\n")
    problems: list[tuple[int, str]] = []
    if not lines or lines[0] != "---":
        return [], [(1, "must start with a '---' frontmatter block")], 1
    entries: list[list] = []
    end = None
    for index in range(1, len(lines)):
        line, number = lines[index], index + 1
        if line == "---":
            end = index
            break
        if line.rstrip() in ("---", "..."):
            problems.append((number, "document markers are not allowed inside frontmatter"))
            continue
        if "\t" in line:
            problems.append((number, "tabs are not allowed in frontmatter"))
            continue
        if not line.strip():
            continue
        if line.startswith(" "):
            if not line.startswith("  ") or not entries:
                problems.append((number, "unexpected indentation"))
                continue
            part = line.strip()
            problem = _value_problem(part, continuation=True)
            if problem:
                problems.append((number, problem))
                continue
            entries[-1][1] = f"{entries[-1][1]} {part}".strip()
            continue
        key, sep, value = line.partition(":")
        if not sep or (value and not value.startswith(" ")):
            problems.append((number, "frontmatter lines must look like 'key: value'"))
            continue
        value = value.strip()
        problem = _value_problem(value, continuation=False)
        if problem:
            problems.append((number, problem))
            continue
        value, problem = _unquote(value)
        if problem:
            problems.append((number, problem))
            continue
        entries.append([key, value, number])
    if end is None:
        problems.append((1, "the frontmatter block is not closed with '---'"))
        return [tuple(e) for e in entries], problems, len(lines) + 1  # type: ignore[misc]
    return [tuple(e) for e in entries], problems, end + 2  # type: ignore[misc]


def _value_problem(value: str, continuation: bool) -> str | None:
    if value.startswith(VALUE_STARTERS) or value == "?" or value.startswith("? "):
        return "YAML flow, block, anchor, alias or tag syntax is not allowed"
    if continuation and value.startswith(("#", "- ", '"', "'")):
        return "continuation lines must be plain text"
    if ": " in value or value.endswith(":") or " #" in value:
        return "values may not contain ': ' or ' #' (YAML would read them differently)"
    return None


def _unquote(value: str) -> tuple[str, str | None]:
    if not value or value[0] not in "\"'":
        return value, None
    quote = value[0]
    inner = value[1:-1]
    if len(value) < 2 or value[-1] != quote or quote in inner or "\\" in inner:
        return value, "quoted values may not contain escapes or inner quotes"
    return inner, None


def _check_frontmatter(path: str, kind: str, text: str, findings: _Findings) -> tuple[dict[str, str], int]:
    entries, problems, body_start = parse_frontmatter(text)
    for number, message in problems:
        findings.error(path, number, "frontmatter", message)
    fields: dict[str, str] = {}
    allowed = FRONTMATTER_KEYS[kind]
    for key, value, number in entries:
        if key in FRONTMATTER_NAMED:
            findings.error(path, number, *FRONTMATTER_NAMED[key])
        elif key == "<<":
            findings.error(path, number, "frontmatter", "YAML merge keys are not allowed")
        elif not _FM_KEY.fullmatch(key):
            findings.error(path, number, "frontmatter", "keys must be lowercase kebab-case and unquoted")
        elif key in fields:
            findings.error(path, number, "frontmatter", "duplicate key (YAML would take the last one)")
        elif key not in allowed:
            findings.error(path, number, "frontmatter-key", f"key not allowed in a {kind} file")
        elif key in FRONTMATTER_BOOLS and value not in ("true", "false"):
            findings.error(path, number, "frontmatter", "this key must be true or false")
        if key not in fields:
            fields[key] = value
    return fields, body_start


def _check_description(path: str, description: str | None, required: bool, findings: _Findings) -> str:
    if not description:
        if required:
            findings.error(path, None, "description", "a description is required")
        return ""
    if len(description) > MAX_DESCRIPTION_CHARS:
        findings.error(path, None, "description", f"description longer than {MAX_DESCRIPTION_CHARS} characters")
    norm = unicodedata.normalize("NFKC", description).casefold()
    if any(p.search(norm) for p in INJECTION_PATTERNS):
        # Descriptions sit in the model context on every turn, used or not.
        findings.error(path, None, "injection-phrase", "the description tries to override instructions")
    return description


def _component_name(path: str, name: str | None, findings: _Findings, what: str) -> bool:
    if not valid_name(name) or _YAML_NON_STRING.fullmatch(name or ""):
        findings.error(path, None, "name", f"{what} must be kebab-case (a-z, 0-9, -), at most 64 characters")
        return False
    return True


def _is_component_path(kind: str, parts: list[str]) -> bool:
    if kind == "skill":
        return len(parts) == 3 and parts[2] == "SKILL.md"
    return len(parts) == 2 and parts[1].endswith(".md")


def _check_components(texts: dict[str, str], findings: _Findings) -> tuple[Component, ...]:
    components: list[Component] = []
    for path, text in sorted(texts.items()):
        kind = _kind(path)
        if kind not in FRONTMATTER_KEYS:
            continue
        parts = path.split("/")
        if not _is_component_path(kind, parts):
            _scan_lines(path, kind, text, 1, findings)  # already refused by the layout check
            continue
        fields, body_start = _check_frontmatter(path, kind, text, findings)
        _scan_lines(path, kind, text, body_start, findings)
        if kind == "skill":
            name = fields.get("name")
            if not name:
                findings.error(path, None, "name", "SKILL.md needs a name")
            elif name != parts[1]:
                findings.error(path, None, "name", "the skill name must equal its folder name")
            elif _component_name(path, name, findings, "the skill name"):
                description = _check_description(path, fields.get("description"), True, findings)
                components.append(Component("skill", name, description))
        elif kind == "command":
            name = parts[1][: -len(".md")]
            if _component_name(path, name, findings, "the command file name"):
                description = _check_description(path, fields.get("description"), False, findings)
                components.append(Component("command", name, description))
        else:
            name = fields.get("name")
            if not name:
                findings.error(path, None, "name", "an agent needs a name")
            elif _component_name(path, name, findings, "the agent name"):
                description = _check_description(path, fields.get("description"), True, findings)
                components.append(Component("agent", name, description))
    seen: set[tuple[str, str]] = set()
    for component in components:
        key = ("agent" if component.kind == "agent" else "invocable", component.name)
        if key in seen:
            findings.error(".", None, "duplicate", f"two {component.kind}s share the name {component.name}")
        seen.add(key)
    if len(components) > MAX_COMPONENTS:
        findings.error(".", None, "too-many-components", f"more than {MAX_COMPONENTS} skills, commands and agents")
    total = sum(len(c.description.encode("utf-8")) for c in components)
    if total > MAX_DESCRIPTION_BYTES:
        findings.error(".", None, "description", f"descriptions longer than {MAX_DESCRIPTION_BYTES // 1024} KiB in total")
    return tuple(components)


def _check_manifest(data: bytes, text: str | None, findings: _Findings) -> tuple[str | None, str | None]:
    path = MANIFEST
    if len(data) > MAX_MANIFEST_BYTES:
        findings.error(path, None, "manifest", f"plugin.json larger than {MAX_MANIFEST_BYTES // 1024} KiB")
        return None, None
    if text is None:
        return None, None

    def no_duplicates(pairs: list[tuple[str, object]]) -> dict:
        keys = [key for key, _ in pairs]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate key")
        return dict(pairs)

    try:
        manifest = json.loads(text, object_pairs_hook=no_duplicates)
    except (ValueError, RecursionError):
        findings.error(path, None, "manifest", "plugin.json is not valid JSON (or has duplicate keys)")
        return None, None
    if not isinstance(manifest, dict):
        findings.error(path, None, "manifest", "plugin.json must be a JSON object")
        return None, None
    for key in manifest:
        if key in MANIFEST_NAMED:
            findings.error(path, None, *MANIFEST_NAMED[key])
        elif key not in MANIFEST_KEYS:
            findings.error(path, None, "manifest-key", "custom component paths and other keys are not supported")
    name = manifest.get("name")
    if not valid_name(name):
        findings.error(path, None, "name", "name must be kebab-case (a-z, 0-9, -) and at most 64 characters")
        name = None
    version = manifest.get("version")
    if version is not None and not (isinstance(version, str) and _VERSION.fullmatch(version)):
        findings.error(path, None, "manifest", "version must be a short string such as 1.0.0")
        version = None
    description = manifest.get("description")
    if description is not None and not (isinstance(description, str) and len(description) <= 500):
        findings.error(path, None, "manifest", "description must be a string of at most 500 characters")
    for key in ("homepage", "repository"):
        value = manifest.get(key)
        if value is not None and not (isinstance(value, str) and value.startswith("https://")
                                      and len(value) <= 200 and not re.search(r"\s", value)):
            findings.error(path, None, "manifest", f"{key} must be an https:// URL of at most 200 characters")
    license_value = manifest.get("license")
    if license_value is not None and not (isinstance(license_value, str) and len(license_value) <= 64):
        findings.error(path, None, "manifest", "license must be a short string")
    keywords = manifest.get("keywords")
    if keywords is not None and not (isinstance(keywords, list) and len(keywords) <= 20
                                     and all(isinstance(k, str) and len(k) <= 50 for k in keywords)):
        findings.error(path, None, "manifest", "keywords must be a list of at most 20 short strings")
    author = manifest.get("author")
    if author is not None:
        good = isinstance(author, str) and len(author) <= 200
        if isinstance(author, dict):
            good = all(k in ("name", "email", "url") and isinstance(v, str) and len(v) <= 200
                       for k, v in author.items())
        if not good:
            findings.error(path, None, "manifest", "author must be a string or an object with name, email and url")
    return (name if isinstance(name, str) else None), (version if isinstance(version, str) else None)


def _digest(files: dict[str, bytes]) -> str:
    sha = hashlib.sha256()
    for rel in sorted(files):
        data = files[rel]
        sha.update(rel.encode("utf-8") + b"\0" + str(len(data)).encode() + b"\0" + data)
    return sha.hexdigest()


def _inspect(root: Path) -> tuple[CheckReport, dict[str, bytes]]:
    """The report plus the exact bytes that were checked (for the copier)."""
    root = Path(os.path.abspath(root))  # not resolved: the root itself must not be a symlink
    findings = _Findings()
    try:
        fd = os.open(root, _DIR_FLAGS)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR) and os.path.islink(root):
            rule, message = "symlink", "the plugin folder is a symbolic link"
        elif exc.errno == errno.ENOTDIR:
            rule, message = "structure", "only plugin folders are supported (no archives or single files)"
        elif exc.errno == errno.ENOENT:
            rule, message = "structure", "the plugin folder does not exist"
        else:
            rule, message = "unreadable", f"cannot open the plugin folder ({type(exc).__name__})"
        return CheckReport(root, None, None, (Finding("error", ".", None, rule, message),), 0, 0, None), {}
    walk = _Walk(findings)
    try:
        walk.run(fd)
    finally:
        os.close(fd)
    _check_layout(walk, findings)
    texts: dict[str, str] = {}
    for path, data in sorted(walk.files.items()):
        text = _decode(path, data, findings)
        if text is None:
            continue
        texts[path] = text
        if _kind(path) not in FRONTMATTER_KEYS:
            _scan_lines(path, _kind(path), text, 1, findings)
    name = version = None
    if MANIFEST in walk.files:
        name, version = _check_manifest(walk.files[MANIFEST], texts.get(MANIFEST), findings)
    components = _check_components(texts, findings)
    digest = None if findings.structural else _digest(walk.files)
    report = CheckReport(
        root=root,
        name=name,
        version=version,
        findings=tuple(findings.items),
        files=len(walk.files),
        total_bytes=walk.total,
        digest=digest,
        components=components,
    )
    return report, walk.files


def check_plugin(root: Path) -> CheckReport:
    """Check a plugin folder. Pure and read-only; nothing from the plugin is executed."""
    return _inspect(Path(root))[0]


# --- locations and trust ------------------------------------------------------------------


def plugins_dir(config: "Config") -> Path:
    return config.state_dir / PLUGINS_DIR


def source_checkout_root() -> Path | None:
    """The repository root when the package runs from a src-layout checkout."""
    root = Path(personal_agent.__file__).resolve().parents[2]
    if (root / "src" / "personal_agent").is_dir() and (root / "pyproject.toml").is_file():
        return root
    return None


def core_plugin_dir(config: "Config") -> Path | None:
    """The shipped core plugin: PA_CORE_PLUGIN_DIR, else plugins/core of a source checkout."""
    if config.core_plugin_off:
        return None
    if config.core_plugin is not None:
        return config.core_plugin.resolve()
    root = source_checkout_root()
    if root is None:
        return None
    candidate = root / "plugins" / CORE_NAME
    return candidate if (candidate / ".claude-plugin" / "plugin.json").is_file() else None


def core_checkout_root(config: "Config") -> Path | None:
    """The checkout root when the core plugin was auto-discovered there (vault must stay out)."""
    if config.core_plugin_off or config.core_plugin is not None or core_plugin_dir(config) is None:
        return None
    return source_checkout_root()


def trust_problem(path: Path) -> str | None:
    """Why a state folder cannot be trusted: symlink, foreign owner, writable by others."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "does not exist"
    except OSError as exc:
        return f"cannot be read ({type(exc).__name__})"
    if stat.S_ISLNK(info.st_mode):
        return "is a symbolic link"
    if not stat.S_ISDIR(info.st_mode):
        return "is not a directory"
    if info.st_uid != os.getuid():
        return "is owned by another user"
    if info.st_mode & 0o022:
        return "is writable by group or others (fix with chmod go-w)"
    return None


def prepare_state_dir(state: Path) -> str | None:
    """Create the state folder (0700) if missing and check it; why it is untrusted, or None.

    Our own state folder created group-writable by a parent mkdir is made private.
    """
    try:
        if not os.path.lexists(state):
            state.mkdir(mode=0o700, parents=True)
        info = os.lstat(state)
        if (stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and info.st_mode & 0o022):
            os.chmod(state, stat.S_IMODE(info.st_mode) & ~0o022)
    except OSError:
        pass
    return trust_problem(state)


def _prepare_plugins_dir(config: "Config") -> Path:
    """Create the plugins folder (0700) after making sure the state folder is private."""
    problem = prepare_state_dir(config.state_dir)
    if problem:
        raise PluginError(f"the state directory {problem}")
    target = plugins_dir(config)
    try:
        os.mkdir(target, 0o700)
    except FileExistsError:
        pass
    problem = trust_problem(target)
    if problem:
        raise PluginError(f"the plugins directory {problem}")
    _remove_leftovers(target)
    return target


def _remove_leftovers(directory: Path, now: float | None = None) -> None:
    """Remove .staging-* and .old-* folders older than an hour (an interrupted enable)."""
    now = time.time() if now is None else now
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return
    for entry in entries:
        if not entry.name.startswith((".staging-", ".old-")):
            continue
        try:
            info = entry.stat(follow_symlinks=False)
            if now - info.st_mtime < STALE_LEFTOVER_S:
                continue
            if stat.S_ISDIR(info.st_mode):
                shutil.rmtree(entry.path)
            else:
                os.unlink(entry.path)
        except OSError as exc:
            log.warning("could not remove a plugin leftover: %s", type(exc).__name__)


# --- registry -------------------------------------------------------------------------------


def _registry_path(config: "Config") -> Path:
    return config.state_dir / REGISTRY_FILE


def _valid_entry(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    digest, version, enabled_at = entry.get("digest"), entry.get("version"), entry.get("enabled_at")
    return (
        isinstance(digest, str) and _DIGEST.fullmatch(digest) is not None
        and (version is None or (isinstance(version, str) and _VERSION.fullmatch(version) is not None))
        and isinstance(enabled_at, (int, float)) and not isinstance(enabled_at, bool)
    )


def load_registry(config: "Config") -> tuple[dict[str, dict], bool]:
    """(entries, corrupt). A missing file is empty; a corrupt one counts as empty too."""
    path = _registry_path(config)
    try:
        fd = os.open(path, _READ_FLAGS)
    except FileNotFoundError:
        return {}, False
    except OSError:
        return {}, True
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_REGISTRY_BYTES:
            return {}, True
        raw = _read_capped(fd, MAX_REGISTRY_BYTES)
    except OSError:
        return {}, True
    finally:
        os.close(fd)
    try:
        data = json.loads((raw or b"").decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return {}, True
    if not isinstance(data, dict) or data.get("version") != REGISTRY_VERSION or not isinstance(data.get("plugins"), dict):
        return {}, True
    entries = {name: entry for name, entry in data["plugins"].items() if valid_name(name) and _valid_entry(entry)}
    return entries, len(entries) != len(data["plugins"])


def _write_registry(config: "Config", entries: dict[str, dict]) -> None:
    path = _registry_path(config)
    payload = json.dumps({"version": REGISTRY_VERSION, "plugins": entries}, indent=2, sort_keys=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".plugins-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- enable / disable / list -------------------------------------------------------------------


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _copy_files(target: Path, files: dict[str, bytes]) -> None:
    """Write the checked bytes into target: new files 0644, folders 0755, nothing followed.

    Never shutil.copytree: no modes, owners or metadata are copied and no symlink is
    followed; only the bytes that passed the check are written.
    """
    root_fd = os.open(target, _DIR_FLAGS)
    try:
        for rel in sorted(files):
            parts = rel.split("/")
            fd = os.dup(root_fd)
            try:
                for part in parts[:-1]:
                    try:
                        os.mkdir(part, 0o755, dir_fd=fd)
                    except FileExistsError:
                        pass
                    child = os.open(part, _DIR_FLAGS, dir_fd=fd)
                    os.fchmod(child, 0o755)
                    os.close(fd)
                    fd = child
                out = os.open(parts[-1], _CREATE_FLAGS, 0o644, dir_fd=fd)
                try:
                    os.fchmod(out, 0o644)
                    _write_all(out, files[rel])
                finally:
                    os.close(out)
            finally:
                os.close(fd)
    finally:
        os.close(root_fd)


def enable_plugin(config: "Config", source: Path, replace: bool = False,
                  expected_digest: str | None = None) -> CheckReport:
    """Check source, copy it into the plugins folder and register its digest.

    expected_digest (from the check the person reviewed) must still match, so the
    plugin cannot change between showing it and enabling it.
    """
    report, files = _inspect(Path(source))
    if not report.ok or report.digest is None or report.name is None:
        raise PluginError("plugin check failed")
    if expected_digest is not None and report.digest != expected_digest:
        raise PluginError("plugin changed since it was checked")
    name = report.name
    if is_reserved(name):
        raise PluginError(f"the name '{name}' is reserved")
    try:
        target = _install(config, report, files, name, replace)
    except OSError as exc:  # disk full, read-only state dir, ...: no traceback, nothing half-installed
        raise PluginError(f"could not enable: {type(exc).__name__}") from exc
    log.info("plugin %s enabled (%d files, %d bytes)", name, report.files, report.total_bytes)
    return check_plugin(target)


def _install(config: "Config", report: CheckReport, files: dict[str, bytes], name: str, replace: bool) -> Path:
    """Copy, swap and register. On any failure the previous state is restored:

    the new copy is removed and a replaced plugin moved back, so the registry and the
    plugins folder always agree.
    """
    directory = _prepare_plugins_dir(config)
    staging: Path | None = Path(tempfile.mkdtemp(prefix=".staging-", dir=directory))
    old: Path | None = None
    target = directory / name
    try:
        assert staging is not None
        _copy_files(staging, files)
        os.chmod(staging, 0o755)
        copied = check_plugin(staging)
        if not copied.ok or copied.digest != report.digest:
            raise PluginError("plugin changed while copying")
        entries, _corrupt = load_registry(config)
        entries[name] = {"digest": report.digest, "version": report.version, "enabled_at": int(time.time())}
        if os.path.lexists(target):
            if not replace:
                raise PluginError("already enabled; use --replace")
            info = os.lstat(target)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise PluginError("the existing plugin entry is not a real directory; remove it by hand")
            old = directory / f".old-{secrets.token_hex(6)}"
            os.replace(target, old)
            try:
                os.replace(staging, target)
            except OSError:
                os.replace(old, target)
                old = None
                raise
        else:
            os.replace(staging, target)
        staging = None
        try:
            _write_registry(config, entries)
        except OSError:
            # Undo the swap: the new copy goes back to staging (removed below), the old one returns.
            staging = directory / f".staging-{secrets.token_hex(6)}"
            os.replace(target, staging)
            if old is not None:
                os.replace(old, target)
                old = None
            raise
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)  # only after the registry agrees with the new copy
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
    return target


def disable_plugin(config: "Config", name: str) -> None:
    if not valid_name(name):
        raise PluginError("invalid plugin name")
    if name == CORE_NAME:
        raise PluginError("the shipped core plugin cannot be disabled here; set PA_CORE_PLUGIN_DIR=off")
    directory = plugins_dir(config)
    entries, _corrupt = load_registry(config)
    target = directory / name
    if not os.path.lexists(target):
        if name in entries:
            del entries[name]
            _write_registry(config, entries)
            return
        raise PluginError("not enabled")
    for path in (config.state_dir, directory):
        problem = trust_problem(path)
        if problem:
            raise PluginError(f"{path.name} {problem}")
    info = os.lstat(target)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise PluginError("the plugin entry is not a real directory; remove it by hand")
    shutil.rmtree(target)  # fd-based on Linux: does not follow symlinks inside
    entries.pop(name, None)
    _write_registry(config, entries)
    log.info("plugin %s disabled", name)


def _status(name: str, report: CheckReport, entries: dict[str, dict]) -> str:
    if not report.ok:
        return "invalid"
    entry = entries.get(name)
    if entry is None or report.name != name:
        return "unregistered"
    if entry["digest"] != report.digest:
        return "changed"
    return "ok"


def _children(directory: Path) -> list[tuple[str, Path]]:
    try:
        names = sorted(entry.name for entry in os.scandir(directory))
    except OSError:
        return []
    return [(name, directory / name) for name in names if not name.startswith(".")]


def list_plugins(config: "Config") -> list[InstalledPlugin]:
    """The core plugin first (shipped), then every folder in the plugins directory."""
    result: list[InstalledPlugin] = []
    core = core_plugin_dir(config)
    if core is not None:
        report = check_plugin(core)
        result.append(InstalledPlugin(report.name or CORE_NAME, core, report.version,
                                      "ok" if report.ok else "invalid", report, shipped=True))
    directory = plugins_dir(config)
    if trust_problem(directory) is None:
        _remove_leftovers(directory)
    entries, _corrupt = load_registry(config)
    for name, path in _children(directory):
        report = check_plugin(path)
        result.append(InstalledPlugin(name, path, report.version, _status(name, report, entries), report))
    return result


def load_for_session(config: "Config") -> tuple[list[LoadedPlugin], list[str]]:
    """The plugins for a chat session (core first) and warnings for the skipped ones.

    Every plugin is checked again; an enabled one must also match the digest it was
    registered with. Names are unique (core wins), and all descriptions together must
    fit MAX_DESCRIPTION_BYTES because they sit in the model context on every turn.
    """
    loaded: list[LoadedPlugin] = []
    warnings: list[str] = []
    used = 0

    def admit(name: str, path: Path, report: CheckReport) -> None:
        nonlocal used
        if any(p.name == name for p in loaded):
            warnings.append(f"plugin {shown(name)} skipped: the name is already in use")
            return
        if used + report.description_bytes > MAX_DESCRIPTION_BYTES:
            warnings.append(f"plugin {shown(name)} skipped: skill descriptions would exceed "
                            f"{MAX_DESCRIPTION_BYTES // 1024} KiB in total")
            return
        used += report.description_bytes
        loaded.append(LoadedPlugin(name, path, report))

    core = core_plugin_dir(config)
    if core is None:
        if not config.core_plugin_off:
            warnings.append("core plugin not found: set PA_CORE_PLUGIN_DIR")
    else:
        report = check_plugin(core)
        if not report.ok or report.name is None:
            warnings.append("core plugin skipped: it failed the check (see `personal-agent plugins check`)")
        else:
            admit(report.name, core, report)

    directory = plugins_dir(config)
    if not os.path.lexists(directory):
        return loaded, warnings
    for path in (config.state_dir, directory):
        problem = trust_problem(path)
        if problem:
            warnings.append(f"enabled plugins skipped: {shown(path)} {problem}")
            return loaded, warnings
    entries, corrupt = load_registry(config)
    if corrupt:
        warnings.append("the plugin registry is unreadable or partly invalid; re-enable the affected plugins")
    for name, path in _children(directory):
        label = shown(name)
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            warnings.append(f"plugin {label} skipped: not a real directory")
            continue
        report = check_plugin(path)
        if not report.ok:
            warnings.append(f"plugin {label} skipped: it failed the check")
            continue
        entry = entries.get(name)
        if entry is None or report.name != name:
            warnings.append(f"plugin {label} skipped: not registered (enable it again)")
            continue
        if entry["digest"] != report.digest:
            warnings.append(f"plugin {label} skipped: changed since enable")
            continue
        admit(name, path, report)
    return loaded, warnings


def invocable(loaded: list[LoadedPlugin]) -> tuple[str, ...]:
    """"<plugin>:<name>" of every skill and command of the loaded plugins."""
    return tuple(sorted(name for plugin in loaded for name in plugin.report.invocable))
