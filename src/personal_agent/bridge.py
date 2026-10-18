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
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

MAX_PROMPT_CHARS = 20_000
DEFAULT_TURN_TIMEOUT_S = 600
ENV_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "TZ")
SKILL_TOOL = "Skill"
# Spike with Claude Code 2.1.302: plugin hooks run arbitrary commands even under
# --restricted and --strict-mcp-config, with the session environment and the config
# dir. "disableAllHooks" turns them off and keeps skills working (verified), and
# --settings still applies under --restricted. With "ask" for Skill, every Skill
# tool call goes through --permission-prompt-tool (verified; without it the Skill
# tool asks nobody and can load built-in skills such as update-config).
HARDENED_SETTINGS = json.dumps(
    {"disableAllHooks": True, "permissions": {"ask": [SKILL_TOOL]}}, separators=(",", ":")
)


class BridgeError(RuntimeError):
    pass


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


class ClaudeSession:
    """A conversation with one Claude Code process. Not thread-safe; one turn at a time."""

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
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=50)

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        if self.running:
            return
        self.config.workdir.mkdir(parents=True, exist_ok=True)
        self.config.config_dir.mkdir(parents=True, exist_ok=True)
        args = build_args(self.config, resume=self.session_id)
        try:
            self._proc = subprocess.Popen(
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
        self._lines = queue.Queue()
        threading.Thread(target=self._pump_stdout, args=(self._proc,), daemon=True).start()
        threading.Thread(target=self._pump_stderr, args=(self._proc,), daemon=True).start()
        log.info("claude started (model %s, resume=%s)", self.config.model, bool(self.session_id))

    def _pump_stdout(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _pump_stderr(self, proc: subprocess.Popen) -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            self._stderr.append(line.rstrip())

    def stderr_tail(self) -> str:
        return "\n".join(self._stderr)

    def send(self, text: str, on_event: Callable[[dict], None] | None = None) -> TurnResult:
        """Send one user message and block until Claude Code reports the turn result."""
        line = user_message(text)
        if not self.running:
            self.start()
        assert self._proc is not None and self._proc.stdin is not None
        try:
            self._proc.stdin.write(line)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise BridgeError(f"Claude Code stopped: {self.stderr_tail()[-300:]}") from exc

        texts: list[str] = []
        while True:
            try:
                raw = self._lines.get(timeout=self.turn_timeout_s)
            except queue.Empty as exc:
                self.close()
                raise BridgeError("Claude Code did not answer in time") from exc
            if raw is None:
                self._proc.wait()
                raise BridgeError(f"Claude Code exited ({self._proc.returncode}): {self.stderr_tail()[-300:]}")
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

    def close(self, timeout_s: float = 10) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=timeout_s)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
