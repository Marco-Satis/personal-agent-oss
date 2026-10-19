"""Bridge to Claude Code: one long-lived `claude -p` process per conversation.

The process speaks stream-json on stdin and stdout. Claude Code runs locked down:
`--restricted` (no shell or code tools, file tools confined to the working
directory), an explicit `--tools` allowlist (restricted mode alone still leaves
tools such as SendMessage that could reach other sessions), only our own MCP
servers (`--strict-mcp-config`), hooks switched off and the Skill tool routed
through the permission tool (`--settings`, see HARDENED_SETTINGS), and a
minimal environment without API keys so there is never a paid fallback when the
subscription quota runs out.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

MAX_PROMPT_CHARS = 20_000
DEFAULT_TURN_TIMEOUT_S = 600
ENV_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "TZ")
SKILL_TOOL = "Skill"
# The main assistant's tool to queue a background task (approval_server.DELEGATE_TOOL_NAME
# on our own "pa" server). Defined here because this module must not import approval.
DELEGATE_TOOL = "mcp__pa__delegate_task"
# Spike with Claude Code 2.1.302: plugin hooks run arbitrary commands even under
# --restricted and --strict-mcp-config, with the session environment and the config
# dir. "disableAllHooks" turns them off and keeps skills working (verified), and
# --settings still applies under --restricted. With "ask" for Skill, every Skill
# tool call goes through --permission-prompt-tool (verified; without it the Skill
# tool asks nobody and can load built-in skills such as update-config). The same
# "ask" rule routes the delegation tool through the permission tool; it is harmless
# when the tool is not offered.
#
# "crossSessionInbound" and the deny rules come from the Claude Code docs
# (cross-session-messaging, checked 2026-10-18): a `-p` session binds an inbox socket
# like an interactive one, so the user's other sessions could message it, and
# SendMessage/ListAgents could reach them. Task and Agent are denied so no session can
# start subagents. NOT yet verified against the real CLI (a manual maintainer spike,
# never in CI). If the spike shows that one of these keys breaks startup, it is not
# simply dropped: the --tools allowlist (outbound) and the out-of-turn tripwire in
# ClaudeSession (inbound) stay, and bare mode is the alternative to evaluate.
HARDENED_SETTINGS = json.dumps(
    {
        "disableAllHooks": True,
        "crossSessionInbound": "refuse",
        "permissions": {
            "ask": [SKILL_TOOL, DELEGATE_TOOL],
            "deny": ["SendMessage", "ListAgents", "Task", "Agent"],
        },
    },
    separators=(",", ":"),
)
OUT_OF_TURN = "output outside a turn (possible message from another session)"


class BridgeError(RuntimeError):
    pass


class UnexpectedOutput(BridgeError):
    """The process produced conversation output while no turn of ours was running.

    Something else started a turn (for example a message from another session).
    router.guarded_send turns this into a safety stop.
    """


@dataclass(frozen=True)
class BridgeConfig:
    workdir: Path
    config_dir: Path
    claude_bin: str = "claude"
    model: str = "sonnet"
    tools: tuple[str, ...] = ("Read", "Glob", "Grep")
    mcp_config: Path | None = None
    permission_tool: str | None = None
    system_prompt_file: Path | None = None
    plugin_dirs: tuple[Path, ...] = ()  # one directory per plugin, never a parent folder
    expected_plugins: tuple[str, ...] = ()  # plugin names from plugin_dirs, for the init tripwire
    expected_mcp: tuple[str, ...] = ()  # MCP server names the init event may list
    allowed_skills: tuple[str, ...] = ()  # "<plugin>:<name>" the Skill tool and "/" messages may use
    persist_session: bool = True  # False: --no-session-persistence (no transcript under config_dir)
    oauth_token: str | None = field(default=None, repr=False)
    # MCP tool names the init tripwire accepts besides `tools` (e.g. the delegation tool).
    # Never passed to --tools, which filters only built-in tools.
    extra_tools: tuple[str, ...] = ()
    max_turns: int | None = None  # --max-turns, bounds unattended helper sessions


def build_args(config: BridgeConfig, resume: str | None = None) -> list[str]:
    """Command line for one locked-down Claude Code process.

    --strict-mcp-config keeps plugin and claude.ai connector MCP servers out (--tools
    does not filter MCP tools), and HARDENED_SETTINGS turns plugin hooks off and routes
    the Skill tool through the permission tool. Neither is configurable. Every plugin
    gets its own --plugin-dir: given a parent folder, the CLI would load every child.
    """
    args = [
        config.claude_bin,
        "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--restricted",
        "--strict-mcp-config",
        "--settings", HARDENED_SETTINGS,
        "--tools", ",".join(config.tools),
        "--model", config.model,
    ]
    if config.mcp_config is not None:
        args += ["--mcp-config", str(config.mcp_config)]
    if config.permission_tool:
        args += ["--permission-prompt-tool", config.permission_tool]
    if config.system_prompt_file is not None:
        args += ["--append-system-prompt-file", str(config.system_prompt_file)]
    for plugin_dir in config.plugin_dirs:
        args += ["--plugin-dir", str(plugin_dir)]
    if not config.persist_session:
        args.append("--no-session-persistence")
    if config.max_turns is not None:
        args += ["--max-turns", str(int(config.max_turns))]
    if resume:
        args += ["--resume", resume]
    return args


def build_env(config: BridgeConfig, base: dict[str, str] | None = None, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Minimal environment for the child. API keys are never passed on."""
    base = os.environ if base is None else base
    env = {key: base[key] for key in ENV_PASSTHROUGH if key in base}
    env["CLAUDE_CONFIG_DIR"] = str(config.config_dir)
    if config.oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = config.oauth_token
    env.update(extra or {})
    return env


@dataclass
class TurnResult:
    text: str
    session_id: str | None
    is_error: bool
    subtype: str
    denials: list[dict]
    cost_usd: float | None
    rate_limit: dict | None


def user_message(text: str) -> str:
    """One stream-json input line."""
    text = text.strip()
    if not text:
        raise BridgeError("empty message")
    if len(text) > MAX_PROMPT_CHARS:
        raise BridgeError(f"message longer than {MAX_PROMPT_CHARS} characters")
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n"


def assistant_text(event: dict) -> str:
    content = (event.get("message") or {}).get("content") or []
    return "".join(block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text")


CONVERSATION_EVENTS = ("assistant", "user", "result")


def _is_conversation_output(line: str) -> bool:
    """True for an event that only a running turn produces (assistant, user, result).

    System and rate-limit events may arrive between turns and are ignored here.
    """
    try:
        event = json.loads(line)
    except ValueError:
        return False
    return isinstance(event, dict) and event.get("type") in CONVERSATION_EVENTS


class ClaudeSession:
    """A conversation with one Claude Code process. One turn at a time.

    `abort()`, `kill()` and `close()` may be called from other threads. Every reap of
    the child (poll/wait) and every signal to its process group happens under
    `_proc_lock`, so a signal never reaches a pid that was already reaped and reused.
    """

    def __init__(
        self,
        config: BridgeConfig,
        extra_env: dict[str, str] | None = None,
        turn_timeout_s: int = DEFAULT_TURN_TIMEOUT_S,
    ) -> None:
        self.config = config
        self.extra_env = extra_env or {}
        self.turn_timeout_s = turn_timeout_s
        self.session_id: str | None = None
        self.last_rate_limit: dict | None = None
        self._proc: subprocess.Popen | None = None
        self._closing: subprocess.Popen | None = None  # the process close() is still stopping
        self._proc_lock = threading.Lock()
        self._aborted = False
        self._in_turn = False
        self._stray = False  # conversation output arrived while no turn of ours was running
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=50)

    @property
    def running(self) -> bool:
        with self._proc_lock:
            return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        if self.running:
            return
        self.config.workdir.mkdir(parents=True, exist_ok=True)
        self.config.config_dir.mkdir(parents=True, exist_ok=True)
        args = build_args(self.config, resume=self.session_id)
        lines: queue.Queue[str | None] = queue.Queue()
        with self._proc_lock:
            if self._aborted:
                raise BridgeError("the session was stopped")
            try:
                proc = subprocess.Popen(
                    args,
                    cwd=self.config.workdir,
                    env=build_env(self.config, extra=self.extra_env),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    bufsize=1,
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                raise BridgeError(f"Claude Code not found: {self.config.claude_bin}") from exc
            self._proc = proc
            self._lines = lines
            self._stray = False
            self._in_turn = False
        threading.Thread(target=self._pump_stdout, args=(proc, lines), daemon=True).start()
        threading.Thread(target=self._pump_stderr, args=(proc,), daemon=True).start()
        log.info("claude started (model %s, resume=%s)", self.config.model, bool(self.session_id))

    def _pump_stdout(self, proc: subprocess.Popen, lines: queue.Queue) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            if not self._in_turn and self._proc is proc and _is_conversation_output(line):
                self._stray = True
            lines.put(line)
        lines.put(None)

    def _pump_stderr(self, proc: subprocess.Popen) -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            self._stderr.append(line.rstrip())

    def stderr_tail(self) -> str:
        return "\n".join(self._stderr)

    def send(self, text: str, on_event: Callable[[dict], None] | None = None) -> TurnResult:
        """Send one user message and block until Claude Code reports the turn result.

        Raises UnexpectedOutput, before writing anything, when the process produced
        conversation output since the last turn ended: a turn we did not send ran in
        between, and its events must never be taken as the answer to this message.
        """
        line = user_message(text)
        if self._stray:
            raise UnexpectedOutput(OUT_OF_TURN)
        if not self.running:
            self.start()
        with self._proc_lock:
            proc = self._proc
            lines = self._lines
        if proc is None or proc.stdin is None:
            raise BridgeError("Claude Code is not running")
        self._in_turn = True
        try:
            return self._turn(proc, lines, line, on_event)
        finally:
            self._in_turn = False

    def _turn(self, proc: subprocess.Popen, lines: queue.Queue, line: str,
              on_event: Callable[[dict], None] | None) -> TurnResult:
        assert proc.stdin is not None
        try:
            proc.stdin.write(line)
            proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise BridgeError(f"Claude Code stopped: {self.stderr_tail()[-300:]}") from exc

        texts: list[str] = []
        while True:
            try:
                raw = lines.get(timeout=self.turn_timeout_s)
            except queue.Empty as exc:
                self.close()
                raise BridgeError("Claude Code did not answer in time") from exc
            if raw is None:
                self._wait(proc, 5)
                raise BridgeError(f"Claude Code exited ({proc.returncode}): {self.stderr_tail()[-300:]}")
            try:
                event = json.loads(raw)
            except ValueError:
                log.debug("ignored non-JSON line from claude")
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "init":
                self.session_id = event.get("session_id") or self.session_id
            elif kind == "assistant":
                texts.append(assistant_text(event))
            elif kind == "rate_limit_event":
                self.last_rate_limit = event.get("rate_limit_info")
            if on_event is not None:
                on_event(event)
            if kind == "result":
                self.session_id = event.get("session_id") or self.session_id
                result_text = event.get("result")
                return TurnResult(
                    text=result_text if isinstance(result_text, str) else "".join(texts),
                    session_id=self.session_id,
                    is_error=bool(event.get("is_error")),
                    subtype=str(event.get("subtype", "")),
                    denials=list(event.get("permission_denials") or []),
                    cost_usd=event.get("total_cost_usd"),
                    rate_limit=self.last_rate_limit,
                )

    # --- stopping the process ---------------------------------------------------

    def _signal(self, proc: subprocess.Popen, sig: int) -> bool:
        """Signal the process group of proc if the process is still alive. Caller holds _proc_lock."""
        if proc.poll() is not None:  # reaped: its pid may already belong to someone else
            return False
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            return False
        return True

    def _wait(self, proc: subprocess.Popen, timeout_s: float) -> bool:
        """Wait up to timeout_s for proc to exit, reaping it only under _proc_lock."""
        end = time.monotonic() + max(0.0, timeout_s)
        while True:
            with self._proc_lock:
                if proc.poll() is not None:
                    return True
            if time.monotonic() >= end:
                return False
            time.sleep(0.05)

    def abort(self) -> None:
        """Stop the running turn from another thread: SIGTERM to the process group.

        Idempotent. The pump thread then sees EOF and send() raises BridgeError. The
        session never starts a process again; the caller still calls close().
        """
        with self._proc_lock:
            self._aborted = True
            proc = self._proc
            if proc is not None:
                self._signal(proc, signal.SIGTERM)

    def interrupt(self) -> None:
        """Stop the running turn only: SIGTERM to the process group, the session stays usable.

        Unlike abort(), a later send() starts a new process that resumes the conversation
        (session_id is kept). For a long-lived session where one turn must end, such as
        "Deny and stop turn" over Telegram.
        """
        with self._proc_lock:
            proc = self._proc
            if proc is not None:
                self._signal(proc, signal.SIGTERM)

    def kill(self) -> None:
        """SIGKILL the process group, also while close() is still stopping it."""
        with self._proc_lock:
            self._aborted = True
            for proc in (self._proc, self._closing):
                if proc is not None:
                    self._signal(proc, signal.SIGKILL)

    def close(self, timeout_s: float = 10) -> None:
        with self._proc_lock:
            proc, self._proc = self._proc, None
            if proc is None:
                return
            self._closing = proc
        try:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass
            if self._wait(proc, timeout_s):
                return
            with self._proc_lock:
                self._signal(proc, signal.SIGTERM)
            if self._wait(proc, 5):
                return
            with self._proc_lock:
                self._signal(proc, signal.SIGKILL)
            self._wait(proc, 2)
        finally:
            with self._proc_lock:
                if self._closing is proc:
                    self._closing = None
