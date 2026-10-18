"""Domains: one memory file per topic for helper tasks (wiki/domains/<domain>.md).

A task may name a domain (`task @research <text>`). The helper then gets that
domain's notes, and the notes of the domains it links to, in front of its message:
delimited, escaped and labelled as reference data (tasks.render_task_prompt). A
helper may add at most three short lines to its own domain file, and only through
an approved, format-checked append (approval.py). Nothing else writes these files,
except the person by hand.

File format:

    # Domain: research

    Links: finance, home-network

    ## Notes
    - 2026-10-18 (task 3f2a9c): restic supports append-only repositories

`Links:` is the first line starting with "Links:" before "## Notes". Links are
directional, at most MAX_LINKS, and depth 1: a linked domain's own links are never
followed. Only the person edits them.

The files are untrusted text: helpers write them from task results that may stem
from mail or the web. All reads go through Vault.read_text (no symlinks, size cap);
domain names are checked against DOMAIN_RE before any path is built. Domain notes
never reach the main session's excerpts or the brief (vault.CONTEXT_SKIP).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime

from .textsafe import strip_invisible
from .vault import DOMAIN_TEMPLATE, DOMAINS_DIR, MAX_DOMAIN_NAME, Vault, VaultError, valid_domain

log = logging.getLogger(__name__)

MAX_LINKS = 5
MAX_DOMAINS = 50
MAX_DOMAIN_READ = 128 * 1024
MAX_SHOW_LINE = 300
OWN_SHARE = 0.6  # of the budget, when linked domains are attached too
CUT_MARK = "…"
NOTES_HEADING = "## notes"
INVALID_NAME = "invalid domain name (lowercase letters, digits and dashes, at most 32 characters)"


@dataclass(frozen=True)
class DomainNote:
    name: str
    links: tuple[str, ...]
    path: str          # vault-relative, e.g. "wiki/domains/research.md"
    text: str          # the "## Notes" section, without invisible or control characters
    problem: str | None = None  # set when the file exists but cannot be attached


@dataclass(frozen=True)
class DomainInfo:
    name: str
    links: tuple[str, ...]
    notes: int
    mtime: float


def domain_path(name: str) -> str:
    """The vault-relative path of a domain file; VaultError for an invalid name."""
    if not valid_domain(name):
        raise VaultError(INVALID_NAME)
    return f"{DOMAINS_DIR}/{name}.md"


def parse_links(raw: str, own: str) -> tuple[str, ...]:
    """The domain keys of a "Links:" value: valid, not own, no duplicates, at most MAX_LINKS."""
    links: list[str] = []
    for part in raw.split(","):
        key = part.strip().lower()
        if not valid_domain(key) or key == own or key in links:
            continue
        links.append(key)
        if len(links) >= MAX_LINKS:
            break
    return tuple(links)


def parse_domain(name: str, text: str) -> tuple[tuple[str, ...], str]:
    """(links, notes text) of a domain file. Without a "## Notes" heading there are no notes."""
    lines = [strip_invisible(raw, keep="\t").rstrip() for raw in text.splitlines()]
    links: tuple[str, ...] = ()
    seen_links = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.lower() == NOTES_HEADING:
            notes = "\n".join(lines[index + 1:]).strip("\n")
            return links, notes
        if not seen_links and stripped.startswith("Links:"):
            seen_links = True
            links = parse_links(stripped[len("Links:"):], name)
    return links, ""


def tail_trim(text: str, limit: int) -> str:
    """At most limit characters of text: whole lines from the end (newest), marked when cut."""
    if limit <= 0 or not text:
        return ""
    if len(text) <= limit:
        return text
    kept: list[str] = []
    size = len(CUT_MARK) + 1
    for line in reversed(text.splitlines()):
        if size + len(line) + 1 > limit:
            break
        kept.append(line)
        size += len(line) + 1
    if not kept:
        return ""
    return CUT_MARK + "\n" + "\n".join(reversed(kept))


def _count_notes(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.lstrip().startswith("- "))


def _cut(line: str, limit: int) -> str:
    return line if len(line) <= limit else line[: limit - 1].rstrip() + CUT_MARK


class Domains:
    """Domain memory files in the vault: list, create a skeleton, read, build helper context."""

    def __init__(self, vault: Vault) -> None:
        self.vault = vault

    def _entries(self) -> dict[str, tuple[float, int]]:
        """{name: (mtime, size)} of the regular files wiki/domains/<valid name>.md."""
        found: dict[str, tuple[float, int]] = {}
        for filename, mtime, size in self.vault.list_files(DOMAINS_DIR):
            stem = filename[:-3] if filename.endswith(".md") else ""
            if valid_domain(stem):
                found[stem] = (mtime, size)
        return found

    def list(self) -> list[DomainInfo]:
        infos: list[DomainInfo] = []
        for name, (mtime, _) in sorted(self._entries().items())[:MAX_DOMAINS]:
            note = self.read(name)
            if note is None:
                continue
            infos.append(DomainInfo(name, note.links, _count_notes(note.text), mtime))
        return infos

    def ensure(self, name: str) -> str:
        """Create the skeleton of a new domain file; never overwrites. "created" or "kept"."""
        rel = domain_path(name)
        existing = self._entries()
        if name not in existing and len(existing) >= MAX_DOMAINS:
            raise VaultError(f"too many domains ({MAX_DOMAINS}); use an existing one")
        state = self.vault.create_file(rel, DOMAIN_TEMPLATE.format(name=name))
        if state == "skipped":  # a symlink or something else sits there
            raise VaultError("unsafe path in vault")
        return state

    def read(self, name: str) -> DomainNote | None:
        """The domain's links and notes; None when the name is invalid or no regular file exists."""
        if not valid_domain(name):
            return None
        rel = domain_path(name)
        text = self.vault.read_text(rel, MAX_DOMAIN_READ)
        if text is None:
            entry = self._entries().get(name)
            if entry is None:
                return None
            problem = "file too large to attach" if entry[1] > MAX_DOMAIN_READ else "file is not readable UTF-8 text"
            return DomainNote(name, (), rel, "", problem)
        links, notes = parse_domain(name, text)
        return DomainNote(name, links, rel, notes)

    def context(self, name: str, budget_chars: int) -> list[DomainNote]:
        """Notes for a helper prompt: own domain first, then linked ones (depth 1), within budget.

        Own notes get up to OWN_SHARE of the budget when linked notes are attached too
        (all of it otherwise); the linked domains split the rest equally, and a share one
        does not use rolls over to the next. Each text keeps its newest whole lines.
        """
        if budget_chars <= 0 or not valid_domain(name):
            return []
        own = self.read(name)
        if own is None or own.problem:
            return []
        linked: list[DomainNote] = []
        for key in own.links:
            note = self.read(key)  # a missing or unreadable linked file is skipped silently
            if note is not None and not note.problem and note.text:
                linked.append(note)
        result: list[DomainNote] = []
        remaining = budget_chars
        own_text = tail_trim(own.text, int(budget_chars * OWN_SHARE) if linked else budget_chars)
        if own_text:
            result.append(replace(own, text=own_text))
            remaining -= len(own_text)
        for index, note in enumerate(linked):
            share = remaining // (len(linked) - index)
            text = tail_trim(note.text, share)
            if text:
                result.append(replace(note, text=text))
                remaining -= len(text)
        return result

    def show(self, name: str, max_lines: int = 20) -> str:
        """Tier-1 view of one domain: header, links and the newest note lines."""
        if not valid_domain(name):
            return INVALID_NAME
        note = self.read(name)
        if note is None:
            return f"no domain {name}"
        lines = [f"domain {name} · {note.path}"]
        if note.problem:
            lines.append(f"({note.problem})")
            return "\n".join(lines)
        lines.append("Links: " + (", ".join(note.links) if note.links else "-"))
        body = [line for line in note.text.splitlines() if line.strip()]
        if not body:
            lines.append("(no notes yet)")
            return "\n".join(lines)
        if len(body) > max_lines:
            lines.append(f"{CUT_MARK} ({len(body) - max_lines} earlier lines)")
        lines += [_cut(line, MAX_SHOW_LINE) for line in body[-max_lines:]]
        return "\n".join(lines)

    def list_text(self) -> str:
        infos = self.list()
        if not infos:
            return "no domains (use task @<name> <text> to start one)"
        return "\n".join(
            f"{info.name:20} {len(info.links)} links  {info.notes} notes  "
            f"updated {datetime.fromtimestamp(info.mtime):%Y-%m-%d}"
            for info in infos
        )


__all__ = [
    "CUT_MARK", "DomainInfo", "DomainNote", "Domains", "MAX_DOMAINS", "MAX_DOMAIN_NAME", "MAX_DOMAIN_READ",
    "MAX_LINKS", "domain_path", "parse_domain", "parse_links", "tail_trim",
]
