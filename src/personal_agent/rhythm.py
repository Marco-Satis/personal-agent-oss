"""The day's rhythm after the morning brief: a midday and an evening update, a weekly review.

All three are off by default (one brief a day is the rule; more messages are opt-in):
PA_RHYTHM_MIDDAY and PA_RHYTHM_EVENING take HH:MM, PA_RHYTHM_WEEKLY takes a weekday and a
time ("fri 16:30"). They belong to the `brief` module.

An update is counts only, built without a model from the mail cache and the task board:
what came in since the last message, what is still unread and needs a person
(deadline/legal, complaint, request), how the background tasks went, and any mailbox
problem. The weekly review counts the week by category and how many requests were
answered: numbers about the process, never about a person.

Each slot is sent at most once per day (the weekly once per week), within
GRACE_S after its time; state_dir/rhythm.json records what went out, so a restart
never repeats a message and a server that was down does not send a stale one.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .mailtriage import category_counts, classify, health_notes, label
from .statefiles import read_private_json, write_private_json

if TYPE_CHECKING:
    from .config import Config
    from .notify import Notifier

log = logging.getLogger(__name__)

STATE_FILE = "rhythm.json"
STATE_VERSION = 1
GRACE_S = 60 * 60
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
NEEDS_PERSON = ("call", "deadline", "complaint", "request")

TEXTS = {
    "en": {"midday": "Midday update", "evening": "Evening update", "weekly": "Weekly review, week {week}",
           "new": "Mail since {since}: {n} new", "open": "Still unread and waiting for you: {items}",
           "nothing_open": "Nothing unread that needs you", "tasks": "Background tasks: {done} done, {failed} failed, "
           "{running} running, {queued} queued", "check": "Check: {note}",
           "week_mail": "Mail this week: {n} ({items})", "answered": "Requests answered: {a} of {n}"},
    "de": {"midday": "Mittags-Update", "evening": "Abend-Update", "weekly": "Wochenrückblick, KW {week}",
           "new": "Mail seit {since}: {n} neu", "open": "Noch ungelesen und für dich: {items}",
           "nothing_open": "Nichts Ungelesenes, das dich braucht", "tasks": "Hintergrund-Aufgaben: {done} fertig, "
           "{failed} fehlgeschlagen, {running} laufen, {queued} wartend", "check": "Prüfen: {note}",
           "week_mail": "Mail diese Woche: {n} ({items})", "answered": "Anfragen beantwortet: {a} von {n}"},
}


def parse_slot_time(raw: str | None) -> tuple[int, int] | None:
    """HH:MM -> (hour, minute); None for unset or empty. Raises ValueError otherwise."""
    if raw is None or not raw.strip():
        return None
    hour, _, minute = raw.strip().partition(":")
    h, m = int(hour), int(minute)
    if not (0 <= h <= 23 and 0 <= m <= 59) or len(minute) != 2:
        raise ValueError("expected HH:MM")
    return h, m


def parse_weekly(raw: str | None) -> tuple[int, int, int] | None:
    """'fri 16:30' -> (weekday 0-6, hour, minute); None for unset or empty. Raises ValueError otherwise."""
    if raw is None or not raw.strip():
        return None
    day, _, clock = raw.strip().lower().partition(" ")
    if day[:3] not in WEEKDAYS:
        raise ValueError("expected a weekday such as 'fri 16:30'")
    slot = parse_slot_time(clock)
    if slot is None:
        raise ValueError("expected a weekday and HH:MM")
    return WEEKDAYS.index(day[:3]), slot[0], slot[1]


@dataclass(frozen=True)
class Snapshot:
    """What an update is built from (tests build it directly)."""
    mails: list            # MessageMeta of the cached window
    health: list[str]      # health notes
    tasks: dict[str, int]  # task board counts by status


def take_snapshot(config: "Config", now: float) -> Snapshot:
    mails: list = []
    health: list[str] = []
    if config.mail_config is not None:
        try:
            from .mail import MailCache

            view = MailCache(config.state_dir).load()
            mails = view.messages()
            health = health_notes(view, now, config.language)
        except Exception as exc:  # noqa: BLE001 - an update never fails because of mail
            log.warning("rhythm: mail cache unreadable (%s)", type(exc).__name__)
    tasks: dict[str, int] = {}
    if config.tasks_enabled:
        try:
            from .tasks import TaskBoard

            tasks = TaskBoard(config.state_dir).counts()
        except Exception as exc:  # noqa: BLE001
            log.warning("rhythm: task board unreadable (%s)", type(exc).__name__)
    return Snapshot(mails, health, tasks)


def _items(counts: list[tuple[str, int]], language: str) -> str:
    return ", ".join(f"{label(c, language)} {n}" for c, n in counts)


def update_lines(snap: Snapshot, since: float, since_text: str, language: str = "en") -> list[str]:
    t = TEXTS.get(language, TEXTS["en"])
    new = [m for m in snap.mails if m.date is not None and m.date > since]
    counts = category_counts((m.subject, m.is_list, m.is_auto) for m in new)
    line = t["new"].format(since=since_text, n=len(new))
    lines = [line + (f" ({_items(counts, language)})" if counts else "")]
    waiting = [m for m in snap.mails if not m.seen
               and classify(m.subject, is_list=m.is_list, is_auto=m.is_auto) in NEEDS_PERSON]
    open_counts = category_counts((m.subject, m.is_list, m.is_auto) for m in waiting)
    lines.append(t["open"].format(items=_items(open_counts, language)) if open_counts else t["nothing_open"])
    if snap.tasks:
        lines.append(t["tasks"].format(done=snap.tasks.get("done", 0), failed=snap.tasks.get("failed", 0),
                                       running=snap.tasks.get("running", 0), queued=snap.tasks.get("queued", 0)))
    lines += [t["check"].format(note=note) for note in snap.health]
    return lines


def weekly_lines(snap: Snapshot, week_start: float, language: str = "en") -> list[str]:
    t = TEXTS.get(language, TEXTS["en"])
    week = [m for m in snap.mails if m.date is not None and m.date >= week_start]
    counts = category_counts((m.subject, m.is_list, m.is_auto) for m in week)
    lines = [t["week_mail"].format(n=len(week), items=_items(counts, language) or "-")]
    requests = [m for m in week if classify(m.subject, is_list=m.is_list, is_auto=m.is_auto) == "request"]
    if requests:
        lines.append(t["answered"].format(a=sum(1 for m in requests if m.answered), n=len(requests)))
    if snap.tasks:
        lines.append(t["tasks"].format(done=snap.tasks.get("done", 0), failed=snap.tasks.get("failed", 0),
                                       running=snap.tasks.get("running", 0), queued=snap.tasks.get("queued", 0)))
    lines += [t["check"].format(note=note) for note in snap.health]
    return lines


class RhythmScheduler:
    """Ticks every 30 s next to the brief scheduler; sends each due slot once."""

    def __init__(self, config: "Config", notifier: "Notifier | None" = None, clock: Callable[[], float] = time.time,
                 snapshot: Callable[["Config", float], Snapshot] = take_snapshot) -> None:
        self.config = config
        self.clock = clock
        self._notifier = notifier
        self._snapshot = snapshot

    @property
    def active(self) -> bool:
        c = self.config
        return any((c.rhythm_midday, c.rhythm_evening, c.rhythm_weekly))

    def _state(self) -> dict:
        data = read_private_json(self.config.state_dir, STATE_FILE, 16 * 1024, STATE_VERSION) or {}
        return {"version": STATE_VERSION, "sent": dict(data.get("sent") or {})}

    def _mark(self, state: dict, slot: str, key: str, now: float) -> None:
        state["sent"][slot] = {"key": key, "at": now}
        Path(self.config.state_dir).mkdir(parents=True, exist_ok=True)
        write_private_json(Path(self.config.state_dir), STATE_FILE, state)

    def _last_message_at(self, state: dict, today: date) -> float:
        """The latest of: this morning's brief time, and any update already sent today."""
        start = datetime.combine(today, datetime.min.time()).timestamp()
        if self.config.brief_time is not None:
            h, m = self.config.brief_time
            start = datetime(today.year, today.month, today.day, h, m).timestamp()
        for entry in state["sent"].values():
            at = entry.get("at") if isinstance(entry, dict) else None
            if isinstance(at, (int, float)) and at >= start and str(entry.get("key", "")).endswith(today.isoformat()):
                start = max(start, float(at))
        return start

    def due(self) -> list[tuple[str, str, float]]:
        """(slot, key, scheduled) for every slot that is due now and not yet sent."""
        now = self.clock()
        today = datetime.fromtimestamp(now).date()
        state = self._state()
        out: list[tuple[str, str, float]] = []
        for slot, hm in (("midday", self.config.rhythm_midday), ("evening", self.config.rhythm_evening)):
            if hm is None:
                continue
            at = datetime(today.year, today.month, today.day, hm[0], hm[1]).timestamp()
            key = f"{slot}:{today.isoformat()}"
            if at <= now < at + GRACE_S and state["sent"].get(slot, {}).get("key") != key:
                out.append((slot, key, at))
        weekly = self.config.rhythm_weekly
        if weekly is not None and today.weekday() == weekly[0]:
            at = datetime(today.year, today.month, today.day, weekly[1], weekly[2]).timestamp()
            year, week, _ = today.isocalendar()
            key = f"weekly:{year}-W{week:02d}"
            if at <= now < at + GRACE_S and state["sent"].get("weekly", {}).get("key") != key:
                out.append(("weekly", key, at))
        return out

    def _messenger(self) -> "Notifier":
        if self._notifier is None:
            from .notify import Notifier

            self._notifier = Notifier.from_config(self.config, run_thread=False)
        return self._notifier

    def tick(self) -> list[str]:
        """The slots sent on this tick."""
        if not self.active:
            return []
        from .notify import rhythm_notice

        sent: list[str] = []
        language = self.config.language
        t = TEXTS.get(language, TEXTS["en"])
        for slot, key, at in self.due():
            now = self.clock()
            today = datetime.fromtimestamp(now).date()
            state = self._state()
            snap = self._snapshot(self.config, now)
            if slot == "weekly":
                monday = today - timedelta(days=today.weekday())
                start = datetime.combine(monday, datetime.min.time()).timestamp()
                title = t["weekly"].format(week=today.isocalendar()[1])
                lines = weekly_lines(snap, start, language)
            else:
                since = self._last_message_at(state, today)
                title = f"{t[slot]} {datetime.fromtimestamp(at).strftime('%H:%M')}"
                lines = update_lines(snap, since, datetime.fromtimestamp(since).strftime("%H:%M"), language)
            # Marked before sending: a crash mid-send must not repeat the message on restart.
            self._mark(state, slot, key, now)
            try:
                self._messenger().send_now("brief", rhythm_notice(title, lines), key=key)
                sent.append(slot)
            except Exception as exc:  # noqa: BLE001 - one failed update never stops the loop
                log.warning("rhythm %s failed: %s", slot, type(exc).__name__)
        return sent


__all__ = ["RhythmScheduler", "Snapshot", "parse_slot_time", "parse_weekly", "update_lines", "weekly_lines"]
