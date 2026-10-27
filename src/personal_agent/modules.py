"""Built-in modules: the core plus the parts an installation switches on.

The core (bridge, approvals, router, usage guard, vault, notifications, config, CLI,
server) always runs. Everything else is a module that ships inside this package and is
listed here with a small manifest. An installation picks its modules with PA_MODULES
(comma-separated names, or "all"); nothing outside this list can be loaded, so a module
never brings foreign code at runtime. Skills a module needs still go through the plugin
checker like any other plugin.

Switching a module off in PA_MODULES overrides its own settings: config.py then treats
it as unset (for example no brief time, no Telegram token).
"""

from __future__ import annotations

from dataclasses import dataclass

KINDS = ("private", "business", "both")


@dataclass(frozen=True)
class Module:
    name: str
    kind: str                       # who it is for: private, business or both
    summary: str
    config_prefix: str              # its PA_* settings
    needs: tuple[str, ...] = ()     # connectors it requires (none built in yet)
    notify_kinds: tuple[str, ...] = ()
    default: bool = True            # on when PA_MODULES is not set


MODULES: tuple[Module, ...] = (
    Module("brief", "both", "morning brief at a fixed time", "PA_BRIEF_", notify_kinds=("brief",)),
    Module("tasks", "both", "background helpers and the task board", "PA_TASK", notify_kinds=("task_done",)),
    Module("domains", "private", "subject areas with their own notes", "PA_DOMAIN_"),
    Module("telegram", "both", "Telegram bot: notices, approvals, chat", "PA_TELEGRAM_"),
)
NAMES = tuple(m.name for m in MODULES)


def by_name(name: str) -> Module | None:
    return next((m for m in MODULES if m.name == name), None)


def parse_modules(raw: str | None) -> tuple[str, ...]:
    """PA_MODULES as a tuple of known names. Raises ValueError on an unknown name."""
    if raw is None or not raw.strip():
        return tuple(m.name for m in MODULES if m.default)
    names = [part.strip().lower() for part in raw.split(",") if part.strip()]
    if names == ["all"]:
        return NAMES
    if names == ["none"]:
        return ()
    unknown = sorted(set(names) - set(NAMES))
    if unknown:
        raise ValueError(f"unknown module(s): {', '.join(unknown)} (known: {', '.join(NAMES)})")
    return tuple(name for name in NAMES if name in names)  # stable order, no duplicates


def describe(enabled: tuple[str, ...]) -> list[str]:
    """One line per known module for `personal-agent modules`."""
    return [
        f"{'on ' if m.name in enabled else 'off'}  {m.name:<10} {m.kind:<8} {m.summary}"
        for m in MODULES
    ]
