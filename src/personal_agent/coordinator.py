"""The coordinator: runs tasks from the board in locked-down helper sessions.

A helper is a separate one-shot `claude -p` session in the vault, with the same
hardening as the main session (--restricted, --strict-mcp-config, hooks off, no API
keys) and its own approval scope on the chat's broker. It gets read tools, plus Edit
when the task has a domain (an approved append to its own domain notes is its only
possible write). It has no Skill, Task or Agent tool and no delegation tool, so a
helper can never start another helper. At most two helpers run at once; the rest wait.
A task with a domain gets that domain's notes, and those of linked domains, in front
of its message as delimited, escaped reference data (domains.py).

Tasks run only while a chat is open (approvals need the person). The helper's final
answer is the result: trusted code here writes it to outputs/tasks/<id>.md. The
result is never handed to the main session; the chat shows a notice, the person reads
the file. A safety stop anywhere (shared SafetyLatch) stops every helper at once.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .bridge import DEFAULT_TURN_TIMEOUT_S, BridgeConfig, BridgeError
from .domains import Domains
from .quota import QuotaGuard
from .router import PaidUsageStop, SafetyLatch, SafetyStop, UnsafeInput, guarded_send
from .status import clean_text
from .tasks import (
    FINAL,
    MAX_PARALLEL,
    Task,
    TaskBoard,
    TaskError,
    TaskLimit,
    clean_error,
    prompt_has_notes,
    render_result_file,
    render_task_prompt,
    result_path,
)
from .textsafe import strip_invisible
from .vault import Vault, VaultError, valid_domain

if TYPE_CHECKING:
    from .approval import ApprovalBroker, ApprovalScope
    from .bridge import ClaudeSession
    from .remote import DiscordWebhook
    from .tasks import Command

log = logging.getLogger(__name__)

HELPER_READ_TOOLS = ("Read", "Glob", "Grep")
HELPER_DOMAIN_TOOLS = ("Read", "Glob", "Grep", "Edit")  # Edit only for an append to its own domain notes
HELPER_MAX_TURNS = 30
QUOTA_FRESH_S = 15 * 60       # older quota data: one helper at a time, as a probe
DEFAULT_MAX_AGE_S = 24 * 3600
CLOSE_KILL_JOIN_S = 2.0
CLOSED = "interrupted (the chat was closed)"
ORPHANED = "interrupted (the task board could not be updated)"
NOTICE_INTERVAL_S = 60
DEFAULT_DOMAIN_CONTEXT_CHARS = 4000


def helper_bridge_config(
    task: Task,
    scope: "ApprovalScope",
    *,
    workdir: Path,
    config_dir: Path,
    claude_bin: str,
    model: str,
    system_prompt_file: Path,
    permission_tool: str,
    server_name: str,
    oauth_token: str | None,
    max_turns: int = HELPER_MAX_TURNS,
) -> BridgeConfig:
    """The locked-down configuration of one helper session."""
    return BridgeConfig(
        workdir=workdir,
        config_dir=config_dir,
        claude_bin=claude_bin,
        model=model,
        tools=HELPER_DOMAIN_TOOLS if task.domain else HELPER_READ_TOOLS,
        mcp_config=scope.mcp_config_path,
        permission_tool=permission_tool,
        system_prompt_file=system_prompt_file,
        plugin_dirs=(),
        expected_plugins=(),
        expected_mcp=(server_name,),
        allowed_skills=(),
        persist_session=False,  # one-shot: no transcript
        oauth_token=oauth_token,
        max_turns=max_turns,
    )


def helper_turn_timeout(approval_timeout_s: int) -> int:
    return max(DEFAULT_TURN_TIMEOUT_S, approval_timeout_s + 120)


@dataclass(frozen=True)
class TaskEvent:
    task_id: str
    status: str  # "done" | "failed" | "cancelled" | "waiting" | "notice" (board-wide, error holds the text)
    result: str | None
    error: str | None
    counts: dict[str, int] = field(default_factory=dict)


def notice_line(event: TaskEvent) -> str:
    """The chat line for a task event; errors are already cleaned."""
    if event.status == "done":
        return f"[task {event.task_id} done: {event.result}]"
    if event.status == "cancelled":
        return f"[task {event.task_id} cancelled]"
    if event.status == "waiting":
        return f"[tasks waiting: {event.error}]"
    if event.status == "notice":
        return f"[tasks: {event.error}]"
    return f"[task {event.task_id} failed: {event.error}]"


class _Worker:
    def __init__(self, task: Task, started: float) -> None:
        self.task = task
        self.started = started
        self.scope: "ApprovalScope | None" = None
        self.session: "ClaudeSession | None" = None
        self.thread: threading.Thread | None = None
        self.abort_reason: str | None = None
        self._lock = threading.Lock()

    def abort(self, reason: str) -> None:
        with self._lock:
            if self.abort_reason is None:
                self.abort_reason = reason
            scope, session = self.scope, self.session
        if scope is not None:
            scope.end_turn()  # a pending prompt of this helper ends as a deny
        if session is not None:
            abort = getattr(session, "abort", None)
            if abort is not None:
                abort()

    def kill(self) -> None:
        with self._lock:
            session = self.session
        kill = getattr(session, "kill", None) if session is not None else None
        if kill is not None:
            kill()

    @property
    def aborted(self) -> str | None:
        with self._lock:
            return self.abort_reason


class Coordinator:
    """Claims tasks from the board and runs each in a helper session (owner chat only)."""

    def __init__(
        self,
        board: TaskBoard,
        broker: "ApprovalBroker",
        guard: QuotaGuard,
        latch: SafetyLatch,
        vault: Vault,
        make_session: Callable[[Task, "ApprovalScope"], "ClaudeSession"],
        notify: Callable[[TaskEvent], None],
        *,
        max_parallel: int = MAX_PARALLEL,
        task_timeout_s: int = 900,
        task_quota_stop: float = 0.8,
        max_age_s: float = DEFAULT_MAX_AGE_S,
        domains: Domains | None = None,
        domain_context_chars: int = DEFAULT_DOMAIN_CONTEXT_CHARS,
        clock: Callable[[], float] = time.monotonic,
        spawn: Callable[[Callable[[], None], str], None] | None = None,
        tick_s: float = 1.0,
    ) -> None:
        self.board = board
        self.broker = broker
        self.guard = guard
        self.latch = latch
        self.vault = vault
        self.make_session = make_session
        self.notify = notify
        self.max_parallel = max(1, min(int(max_parallel), MAX_PARALLEL))
        self.task_timeout_s = task_timeout_s
        self.task_quota_stop = task_quota_stop
        self.max_age_s = max_age_s
        self.domains = domains
        self.domain_context_chars = max(0, int(domain_context_chars))
        self.clock = clock
        self._spawn = spawn
        self.tick_s = tick_s
        self.owner = False
        self._lock = threading.Lock()
        self._workers: dict[str, _Worker] = {}
        self._pending_finish: dict[str, tuple] = {}  # task id -> outcome whose board update failed
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._notified: str | None = None  # the last waiting/paused reason told to the person

    # --- lifecycle ---

    def start(self, run_thread: bool = True) -> bool:
        """Take the owner lock, recover interrupted tasks and start the dispatcher. False: not the owner."""
        if not self.board.acquire_owner():
            return False
        self.owner = True
        try:
            self.board.recover_interrupted()
        except TaskError as exc:
            log.warning("task recovery failed: %s", type(exc).__name__)
        self.latch.add_listener(self._on_latch)
        if run_thread:
            self._thread = threading.Thread(target=self._loop, name="task-coordinator", daemon=True)
            self._thread.start()
        return True

    def wake(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - the dispatcher must keep running
                log.warning("task dispatcher round failed: %s", type(exc).__name__)
            self._wake.wait(self.tick_s)
            self._wake.clear()

    def _on_latch(self, reason: str) -> None:
        """A safety stop anywhere: stop every running helper now, not only new claims."""
        for worker in self._running():
            worker.abort("stopped: safety stop elsewhere")
        self.wake()

    def _running(self) -> list[_Worker]:
        with self._lock:
            return list(self._workers.values())

    def running_count(self) -> int:
        with self._lock:
            return len(self._workers)

    def approval_pending(self, task_id: str) -> bool:
        with self._lock:
            worker = self._workers.get(task_id)
        scope = worker.scope if worker is not None else None
        return scope is not None and self.broker.scope_pending(scope)

    def _notify(self, event: TaskEvent) -> None:
        try:
            self.notify(event)
        except Exception as exc:  # noqa: BLE001 - a notice must never stop the work
            log.warning("task notice failed: %s", type(exc).__name__)

    def _counts(self) -> dict[str, int]:
        try:
            return self.board.counts()
        except TaskError:
            return {}

    def _notify_waiting(self, reason: str) -> None:
        if reason != self._notified:
            self._notified = reason
            self._notify(TaskEvent("-", "waiting", None, clean_error(reason), self._counts()))

    # --- one dispatcher round ---

    def tick(self) -> None:
        if self._stop.is_set():
            return
        self._retry_finishes()
        tasks, corrupt = self.board.load()
        by_id = {t.id: t for t in tasks}
        now = self.clock()
        missing: list[Task] = []
        for worker in self._running():
            record = by_id.get(worker.task.id)
            if corrupt:
                pass  # unreadable: decide nothing from it; the next mutation sets it aside
            elif record is None:
                # A running record never disappears on its own (pruning drops only final
                # tasks): the board was set aside or replaced. Keep the helper, restore it.
                missing.append(worker.task)
                continue
            elif record.cancel_requested:
                worker.abort("cancelled")
                continue
            if now - worker.started > self.task_timeout_s:
                worker.abort(f"timed out after {self.task_timeout_s} s")
        try:
            if missing:
                self.board.restore(missing)
            if not corrupt and any(t.status == "running" for t in tasks):
                self._reconcile(tasks)
            if any(t.status in ("queued", "waiting") and t.cancel_requested for t in tasks):
                for task_id in self.board.cancel_requested_open():
                    self._notify(TaskEvent(task_id, "cancelled", None, None, self._counts()))
            for task_id in self.board.expire(self.max_age_s):
                self._notify(TaskEvent(task_id, "failed", None, "expired before it ran", self._counts()))
            reason = self.latch.reason
            if reason:
                for worker in self._running():
                    worker.abort("stopped: safety stop elsewhere")
                paused = f"paused: {reason}"
                if self.board.set_waiting(paused) or self._notified != paused:
                    self._notify_waiting(paused)
                return
            self._claim()
        except TaskError as exc:
            log.warning("task dispatcher: %s", type(exc).__name__)
        finally:
            self._report_set_aside()

    def _report_set_aside(self) -> None:
        for name in self.board.take_set_aside():
            self._notify(TaskEvent("-", "notice", None,
                                   f"the task board was unreadable and was set aside as {name}", self._counts()))

    def _retry_finishes(self) -> None:
        """Record outcomes whose board update failed earlier (disk full, lock busy)."""
        with self._lock:
            pending = list(self._pending_finish.items())
        for task_id, outcome in pending:
            status, error, result, sha, denials = outcome
            try:
                self.board.finish(task_id, status, result=result, result_sha256=sha, error=error, denials=denials)
            except TaskError as exc:
                log.warning("task %s: could not record the result yet (%s)", task_id, type(exc).__name__)
                continue
            with self._lock:
                self._pending_finish.pop(task_id, None)

    def _reconcile(self, tasks: list[Task]) -> None:
        """A running record that no worker of this owner runs, and no pending outcome covers,
        is orphaned (e.g. a status change failed): end it so cancel and the open-task cap work.

        Outcomes of finished helpers wait in _pending_finish (with their result path and
        sha256), so an orphan here never had a result written by this coordinator.
        """
        with self._lock:
            known = set(self._workers) | set(self._pending_finish)
        for task in tasks:
            if task.status != "running" or task.id in known:
                continue
            if self.board.finish(task.id, "failed", error=ORPHANED, denials=task.denials) is not None:
                self._notify(TaskEvent(task.id, "failed", None, ORPHANED, self._counts()))

    def _claim(self) -> None:
        while not self._stop.is_set():
            running = self.running_count()
            if running >= self.max_parallel:
                return
            check = self.guard.check(stop_at=self.task_quota_stop, require_fresh_s=QUOTA_FRESH_S)
            if not check.ok:
                if self.board.set_waiting(check.reason):
                    self._notify_waiting(check.reason)
                return
            if not check.fresh and running >= 1:
                return  # without fresh quota data, one helper at a time
            try:
                task = self.board.claim_next()
            except TaskLimit as exc:
                if self.board.set_waiting(str(exc)):
                    self._notify_waiting(str(exc))
                return
            if task is None:
                return
            self._notified = None
            self._start_worker(task)

    def _start_worker(self, task: Task) -> None:
        worker = _Worker(task, self.clock())
        with self._lock:
            # close() may have run while claim_next waited for the board lock: a worker
            # started now would run after the chat closed, unsupervised and using quota.
            stopping = self._stop.is_set()
            if not stopping:
                self._workers[task.id] = worker
        if stopping:
            try:
                self.board.set_status(task.id, "queued")
            except TaskError as exc:
                log.warning("task %s: could not put it back in the queue (%s)", task.id, type(exc).__name__)
            return
        if self._spawn is not None:
            self._spawn(lambda: self._run(worker), f"task-{task.id}")
            return
        thread = threading.Thread(target=self._run, args=(worker,), name=f"task-{task.id}", daemon=True)
        worker.thread = thread
        thread.start()

    # --- one helper ---

    def _run(self, worker: _Worker) -> None:
        task = worker.task
        outcome: tuple[str, str | None, str | None, str | None, int] | None = ("failed", "internal error", None, None, 0)
        aborted_or_stopped = False
        try:
            outcome, aborted_or_stopped = self._work(worker)
        except Exception as exc:  # noqa: BLE001
            log.warning("task %s: internal error (%s)", task.id, type(exc).__name__)
            outcome = ("failed", f"internal error ({type(exc).__name__})", None, None, 0)
        finally:
            scope, session = worker.scope, worker.session
            if scope is not None:
                scope.end_turn()
            if session is not None:
                try:
                    session.close(timeout_s=0 if aborted_or_stopped or worker.aborted else 10)
                except Exception as exc:  # noqa: BLE001
                    log.warning("task %s: closing the helper failed (%s)", task.id, type(exc).__name__)
            if scope is not None:
                try:
                    self.broker.close_scope(scope)
                except Exception as exc:  # noqa: BLE001
                    log.warning("task %s: closing the approval scope failed (%s)", task.id, type(exc).__name__)
            event = None
            if outcome is not None:
                status, error, result, sha, denials = outcome
                try:
                    self.board.finish(task.id, status, result=result, result_sha256=sha, error=error,
                                      denials=denials)
                except TaskError as exc:
                    # Kept and retried on later ticks: otherwise the record stays "running"
                    # with no worker, and cancel, the watchdog and the open-task cap never end it.
                    log.warning("task %s: could not record the result (%s)", task.id, type(exc).__name__)
                    with self._lock:
                        self._pending_finish[task.id] = outcome
                event =TaskEvent(task.id, status, result_path(task.id) if result else None,
                                  None if error is None else clean_error(error), self._counts())
            with self._lock:
                self._workers.pop(task.id, None)
            if event is not None:
                self._notify(event)
            self.wake()

    def _work(self, worker: _Worker) -> tuple[tuple[str, str | None, str | None, str | None, int] | None, bool]:
        """Run the helper; ((status, error, result, sha256, denials) or None to keep it waiting, stopped)."""
        task = worker.task
        if self._halted(worker):
            return self._aborted(worker), True
        try:
            scope = self.broker.open_scope(
                f"task-{task.id}", kind="helper", task_id=task.id, title=task.title,
                domain=task.domain, delegated=task.delegated,
            )
        except RuntimeError:
            return ("failed", "could not open an approval scope", None, None, 0), False
        worker.scope = scope
        session = self.make_session(task, scope)
        worker.session = session
        if self._halted(worker):
            return self._aborted(worker), True
        check = self.guard.check(stop_at=self.task_quota_stop)
        if not check.ok:  # the quota changed since the claim
            try:
                new = self.board.set_status(task.id, "waiting", error=check.reason)
            except TaskError as exc:
                log.warning("task %s: could not set it back to waiting (%s)", task.id, type(exc).__name__)
                return ("failed", f"stopped: {check.reason}", None, None, 0), False
            if new is not None and new.status == "cancelled":
                # The person cancelled it while it was being claimed.
                self._notify(TaskEvent(task.id, "cancelled", None, None, self._counts()))
                return None, False
            self._notify_waiting(check.reason)
            return None, False
        prompt = render_task_prompt(task, self._domain_notes(task))
        scope.begin_turn()
        scope.set_turn_context("helper task" + ("; domain notes in this turn" if prompt_has_notes(prompt) else ""))
        if self._halted(worker):
            return self._aborted(worker), True
        try:
            result = guarded_send(session, prompt, self.guard, on_event=scope.observe, slash_commands=frozenset())
        except UnsafeInput as exc:
            return ("failed", f"refused: {exc}", None, None, 0), True
        except PaidUsageStop as exc:
            return ("failed", f"stopped: {exc}", None, None, 0), True
        except SafetyStop as exc:
            self.latch.trip(str(exc))  # the main chat and all claims stop until restart
            return ("failed", f"stopped: {exc}", None, None, 0), True
        except BridgeError as exc:
            if self._halted(worker):
                return self._aborted(worker), True
            return ("failed", "Claude Code failed: " + clean_text(str(exc), 120), None, None, 0), True
        if self._halted(worker):
            return self._aborted(worker), True
        denials = len(result.denials)
        if result.is_error:
            if result.subtype == "error_max_turns":
                return ("failed", "turn limit reached", None, None, denials), False
            return ("failed", f"turn ended with an error ({clean_text(result.subtype, 40)})", None, None, denials), False
        text = strip_invisible(result.text or "", keep="\n\t")
        content = render_result_file(task, text, time.time(), denials)
        try:
            path = self.vault.write_task_result(task.id, content)
        except VaultError:
            return ("failed", "could not write the result", None, None, denials), False
        written = self.vault.read_task_result(task.id)
        sha = hashlib.sha256((written if written is not None else content).encode("utf-8")).hexdigest()
        return ("done", None, path, sha, denials), False

    def _domain_notes(self, task: Task) -> list:
        """The task's domain notes and those of linked domains; [] without a domain or budget."""
        if self.domains is None or not task.domain or self.domain_context_chars <= 0:
            return []
        try:
            return self.domains.context(task.domain, self.domain_context_chars)
        except Exception as exc:  # noqa: BLE001 - notes are optional, the task still runs
            log.warning("task %s: domain notes skipped (%s)", task.id, type(exc).__name__)
            return []

    def _halted(self, worker: _Worker) -> bool:
        """Whether the worker must stop: aborted, or the coordinator is closing."""
        if self._stop.is_set():
            worker.abort(CLOSED)  # keeps an earlier reason; also ends the scope's turn
        return worker.aborted is not None

    @staticmethod
    def _aborted(worker: _Worker) -> tuple[str, str | None, None, None, int]:
        reason = worker.aborted or "stopped"
        if reason == "cancelled":
            return "cancelled", None, None, None, 0
        return "failed", reason, None, None, 0

    def close(self, join_s: float = 10.0) -> None:
        """Stop the dispatcher and every helper: running tasks become failed, never requeued."""
        self._stop.set()
        self.wake()
        end = time.monotonic() + max(0.0, join_s)
        for worker in self._running():
            worker.abort(CLOSED)
        # The dispatcher first: it may be inside claim_next. Once _stop is set it starts no
        # new worker (_start_worker), so after this join the worker set can only shrink.
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(max(0.0, end - time.monotonic()))
        for worker in self._running():
            worker.abort(CLOSED)
        for worker in self._running():
            if worker.thread is not None:
                worker.thread.join(max(0.0, end - time.monotonic()))
        leftover = self._running()
        if leftover:
            # Processes that ignored SIGTERM must not outlive the chat, using quota.
            for worker in leftover:
                worker.kill()
            for worker in leftover:
                if worker.thread is not None:
                    worker.thread.join(CLOSE_KILL_JOIN_S)
        if self.owner:
            self._retry_finishes()  # last chance; otherwise the next owner marks them interrupted
            self.board.release_owner()
            self.owner = False


# --- the facade for the router and for delegation ---------------------------------------


class TaskService:
    """Tier-1 task commands and delegation, on top of the board and (in the owner chat) the coordinator."""

    def __init__(self, board: TaskBoard, coordinator: Coordinator | None, latch: SafetyLatch,
                 vault: Vault | None = None, domains: Domains | None = None,
                 standby: Coordinator | None = None) -> None:
        self.board = board
        self.coordinator = coordinator
        self.latch = latch
        self.vault = vault
        if domains is None and vault is not None:
            domains = Domains(vault)
        self.domains = domains
        self._standby = standby if coordinator is None else None  # not started: another chat owns the board
        self._takeover_lock = threading.Lock()

    def try_take_over(self) -> bool:
        """In a chat that is not the owner: become the owner once the other chat has closed.

        True only on the call that took over. Cheap (one non-blocking flock); called
        before each prompt and each task command.
        """
        with self._takeover_lock:
            standby = self._standby
            if self.coordinator is not None or standby is None:
                return False
            try:
                started = standby.start()
            except TaskError as exc:
                log.warning("task board takeover failed: %s", type(exc).__name__)
                return False
            if not started:
                return False
            self.coordinator, self._standby = standby, None
            log.info("task board taken over by this chat")
            return True

    def _set_aside_lines(self) -> list[str]:
        return [f"[tasks: the task board was unreadable and was set aside as {name}]"
                for name in self.board.take_set_aside()]

    def _ensure_domain(self, domain: str) -> None:
        """Trusted code creates the skeleton of a new domain file (never overwrites, no symlinks)."""
        if self.domains is not None:
            self.domains.ensure(domain)

    def add(self, text: str, domain: str | None, origin: str = "chat") -> tuple[Task, str]:
        if domain is not None:
            if not valid_domain(domain):
                raise TaskError("invalid domain name (lowercase letters, digits and dashes)")
            self._ensure_domain(domain)
        id_taken = self.vault.task_result_exists if self.vault is not None else None
        task = self.board.add(text, domain, origin, id_taken=id_taken)
        if self.coordinator is not None:
            self.coordinator.wake()
        return task, self._added_line(task)

    def _added_line(self, task: Task) -> str:
        tasks, _ = self.board.load()
        running = sum(1 for t in tasks if t.status == "running")
        ahead = sum(1 for t in tasks if t.status in ("queued", "waiting") and t.created_at < task.created_at)
        line = f"task {task.id} queued ({running} running, {ahead} ahead) · result: {result_path(task.id)}"
        reason = self.latch.reason
        if reason:
            line += f" · tasks are paused: {reason}"
        if self.coordinator is None:
            line += " · it runs in the other open chat; its approvals appear there"
        return line

    def delegate(self, text: str, domain: str | None) -> str:
        """Called by the broker after an approved delegate_task; returns only the task id."""
        task, _ = self.add(text, domain, origin="assistant")
        return task.id

    def command(self, command: "Command") -> tuple[str, bool]:
        notes = ["[tasks: this chat now runs the task board]"] if self.try_take_over() else []
        reply, is_error = self._command(command)
        notes += self._set_aside_lines()
        return "\n".join(notes + [reply]), is_error

    def _command(self, command: "Command") -> tuple[str, bool]:
        try:
            if command.kind == "task_add":
                return self.add(command.arg, command.domain)[1], False
            if command.kind == "tasks":
                return self.list_text(), False
            if command.kind == "task_show":
                return self.show_text(command.arg), False
            if command.kind == "task_cancel":
                return self.cancel_text(command.arg), False
        except (TaskError, VaultError) as exc:
            return str(exc), True
        return "unknown task command", True

    def _status(self, task: Task) -> str:
        if (task.status == "running" and self.coordinator is not None
                and self.coordinator.approval_pending(task.id)):
            return "running·approval"
        return task.status

    def list_text(self) -> str:
        tasks = self.board.list()
        if not tasks:
            return "no tasks"
        open_tasks = [t for t in tasks if t.status not in FINAL]
        final = [t for t in tasks if t.status in FINAL][:5]
        now = time.time()
        lines = []
        for task in (open_tasks + final)[:20]:
            age = max(0, int((now - task.created_at) // 60))
            mark = "[delegated] " if task.delegated else ""
            lines.append(f"{task.id}  {self._status(task):10} {task.domain or '-':12} {age:>4} min  "
                         f"{mark}{clean_text(task.title, 60)}")
        return "\n".join(lines)

    def show_text(self, prefix: str) -> str:
        task = self.board.get(prefix)
        lines = [
            f"task {task.id}" + (" [delegated by the assistant]" if task.delegated else ""),
            f"status: {self._status(task)}",
            f"domain: {task.domain or '-'}",
            f"origin: {task.origin}",
            f"created: {_local(task.created_at)}" + (f" · finished: {_local(task.finished_at)}"
                                                     if task.finished_at else ""),
            f"request: {clean_text(task.text, 300)}",
        ]
        if task.result:
            lines.append(f"result: {task.result}")
        if task.error:
            lines.append(f"error: {clean_error(task.error)}")
        if task.denials:
            lines.append(f"actions not approved: {task.denials}")
        if task.status == "done" and self.vault is not None:
            text = self.vault.read_task_result(task.id)
            if text is None:
                lines.append("(the result file is missing or unreadable)")
            else:
                if task.result_sha256 and hashlib.sha256(text.encode("utf-8")).hexdigest() != task.result_sha256:
                    lines.append("(the file changed since the helper wrote it)")
                lines.append("[result preview: data written by a helper]")
                lines.append(_preview(text))
        return "\n".join(lines)

    def cancel_text(self, prefix: str) -> str:
        task = self.board.request_cancel(prefix)
        if self.coordinator is not None:
            self.coordinator.wake()
        if task.status == "cancelled":
            return f"task {task.id} cancelled"
        return f"task {task.id}: stopping …"


def _local(ts: float | None) -> str:
    return "-" if ts is None else time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _preview(text: str, max_lines: int = 15, max_chars: int = 1200) -> str:
    lines = strip_invisible(text, keep="\n\t").splitlines()
    shown = lines[:max_lines]
    preview = "\n".join(shown)
    cut = len(lines) > max_lines
    if len(preview) > max_chars:
        preview, cut = preview[:max_chars], True
    return preview + ("\n…(cut)" if cut else "")


# --- Discord: counts only -----------------------------------------------------------------


class TaskNotifier:
    """Posts task counts to the Discord webhook, at most once per interval. Never ids,
    titles, domains, errors or paths: the webhook channel is outside the person's machine."""

    def __init__(self, webhook: "DiscordWebhook", board: TaskBoard | None = None,
                 min_interval_s: float = NOTICE_INTERVAL_S, clock: Callable[[], float] = time.monotonic,
                 run_thread: bool = True) -> None:
        self.webhook = webhook
        self.board = board
        self.min_interval_s = min_interval_s
        self.clock = clock
        self._lock = threading.Lock()
        self._pending = {"done": 0, "failed": 0, "cancelled": 0}
        self._last_post: float | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        if run_thread:
            self._thread = threading.Thread(target=self._loop, name="task-notice", daemon=True)
            self._thread.start()

    def record(self, event: TaskEvent) -> None:
        if event.status not in self._pending:
            return
        with self._lock:
            self._pending[event.status] += 1
        self._wake.set()

    def text(self) -> str | None:
        with self._lock:
            pending = dict(self._pending)
        if not any(pending.values()):
            return None
        running = queued = 0
        if self.board is not None:
            try:
                counts = self.board.counts()
                running = counts.get("running", 0)
                queued = counts.get("queued", 0) + counts.get("waiting", 0)
            except TaskError:
                pass
        return (f"Tasks: {pending['done']} done, {pending['failed']} failed, {pending['cancelled']} cancelled"
                f" · {running} running, {queued} queued")

    def flush(self, force: bool = False) -> bool:
        """Post pending counts if the interval allows (or force). True when something was posted."""
        now = self.clock()
        if not force and self._last_post is not None and now - self._last_post < self.min_interval_s:
            return False
        text = self.text()
        if text is None:
            return False
        with self._lock:
            sent = dict(self._pending)
        if not self.webhook.post(text):
            return False  # counts are kept for the next post
        with self._lock:
            for key, value in sent.items():
                self._pending[key] -= value
        self._last_post = now
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(5.0)
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                self.flush()
            except Exception as exc:  # noqa: BLE001
                log.warning("task notice post failed: %s", type(exc).__name__)

    def close(self, timeout_s: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout_s)
        try:
            self.flush(force=True)
        except Exception as exc:  # noqa: BLE001 - best effort
            log.warning("task notice post failed: %s", type(exc).__name__)


__all__ = [
    "Coordinator", "TaskEvent", "TaskNotifier", "TaskService", "helper_bridge_config", "helper_turn_timeout",
    "notice_line",
]
