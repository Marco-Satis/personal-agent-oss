"""Talk to the assistant from a phone shortcut (iOS Shortcuts, Android Tasker): POST /chat.

A thin client, no logic on the phone (Marco 17.11.2026): the shortcut dictates, posts the text
and reads the answer aloud. Everything runs here.

Access (PA_CHAT_DEVICES=on, default off), three locks:
1. Only tailnet traffic through `tailscale serve`: the request comes from loopback and carries the
   `Tailscale-User-Login` identity header. Tailscale removes such headers from incoming requests
   and never sets them for Funnel (public) traffic, so a Funnel request is refused with 404. The
   header is a gate, not a proof: a local process could set it, so (2) still applies.
2. A device token per phone (`personal-agent devices add <name>`), shown once, stored only as a
   SHA-256 hash in state_dir/devices.json (0600). Revoke one with `personal-agent devices revoke`.
3. The token only chats: the turn runs through the same Router as ntfy, with an approval channel
   that denies every level-2/3 request and says where to approve. The focus tool works on the
   person's own lists without an approval (approval.py).

Long turns: POST waits up to WAIT_S for the reply (200 {"reply"}), else answers 202 {"id"} and
GET /chat/<id>?wait=25 picks the reply up later. Replies stay JOB_TTL_S, then they are gone.
Message text is never logged.
"""

from __future__ import annotations

import collections
import hashlib
import hmac
import logging
import queue
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .statefiles import read_private_json, write_private_json

if TYPE_CHECKING:
    from .approval import ApprovalBroker
    from .assistant import AssistantRuntime
    from .config import Config

log = logging.getLogger(__name__)

DEVICES_FILE = "devices.json"
DEVICES_VERSION = 1
TOKEN_PREFIX = "pad_"
MAX_DEVICES = 10
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
JOB_ID_RE = re.compile(r"[A-Za-z0-9_-]{16,32}")
MAX_TEXT = 2000
MAX_QUEUED = 5
MAX_JOBS = 20
JOB_TTL_S = 600
WAIT_S = 25.0
MAX_PER_MIN = 10
PIN_LIKE = re.compile(r"[0-9]{6,32}")
IDLE_CLOSE_S = 1800

TEXTS = {
    "de": {
        "approval": "Das braucht eine Freigabe ({tool}). Über den Kurzbefehl gibt es noch keine Freigaben: "
                    "bitte im Chat oder auf der Freigabe-Seite erledigen.",
        "pin": "Das sieht wie eine PIN aus. PINs gehören nicht hierher; die Nachricht wurde nicht verarbeitet.",
        "limit": "Stundenlimit für Modell-Antworten erreicht. Feste Befehle (fokus, inbox …) gehen weiter.",
        "unavailable": "Der Assistent ist gerade nicht erreichbar ({reason}).",
        "error": "Die Antwort ist fehlgeschlagen ({reason}).",
        "no_login": "Fragen ans Modell gehen erst, wenn der Agent einen eigenen Claude-Login hat. "
                    "Feste Befehle (fokus, inbox, einkauf …) gehen schon.",
    },
    "en": {
        "approval": "This needs an approval ({tool}). The shortcut has no approvals yet: please use the chat "
                    "or the approval page.",
        "pin": "That looks like a PIN. PINs do not belong here; the message was not handled.",
        "limit": "Hourly limit for model replies reached. Fixed commands (focus, inbox …) still work.",
        "unavailable": "The assistant is not available right now ({reason}).",
        "error": "The reply failed ({reason}).",
        "no_login": "Questions to the model work once the agent has its own Claude login. "
                    "Fixed commands (focus, inbox, shopping …) already work.",
    },
}


class DeviceError(RuntimeError):
    pass


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class DeviceStore:
    """Device tokens, hashed, in state_dir/devices.json (0600)."""

    def __init__(self, state_dir: Path, clock: Callable[[], float] = time.time) -> None:
        self.state_dir = Path(state_dir)
        self.clock = clock
        self._lock = threading.Lock()

    def _load(self) -> dict:
        data = read_private_json(self.state_dir, DEVICES_FILE, 64 * 1024, DEVICES_VERSION) or {}
        devices = data.get("devices")
        return {k: v for k, v in devices.items() if isinstance(v, dict)} if isinstance(devices, dict) else {}

    def _save(self, devices: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_private_json(self.state_dir, DEVICES_FILE, {"version": DEVICES_VERSION, "devices": devices})

    def add(self, name: str) -> str:
        """A new token for a device name; shown once, only its hash is kept."""
        if not NAME_RE.fullmatch(name or ""):
            raise DeviceError("device names: lowercase letters, digits and dashes, at most 32 characters")
        with self._lock:
            devices = self._load()
            if name in devices and not devices[name].get("revoked"):
                raise DeviceError(f"device {name} exists; revoke it first")
            if sum(1 for d in devices.values() if not d.get("revoked")) >= MAX_DEVICES:
                raise DeviceError(f"at most {MAX_DEVICES} devices")
            token = TOKEN_PREFIX + secrets.token_urlsafe(32)
            devices[name] = {"hash": _hash(token), "created": round(self.clock()), "revoked": False}
            self._save(devices)
        return token

    def revoke(self, name: str) -> bool:
        with self._lock:
            devices = self._load()
            if name not in devices or devices[name].get("revoked"):
                return False
            devices[name]["revoked"] = True
            self._save(devices)
        return True

    def list(self) -> list[tuple[str, bool, int]]:
        """(name, active, created) per device; never a token or hash."""
        return [(name, not d.get("revoked"), int(d.get("created") or 0)) for name, d in sorted(self._load().items())]

    def verify(self, token: str) -> str | None:
        """The device name for a token, or None. Compares against every active device."""
        if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX) or len(token) > 100:
            return None
        probe = _hash(token)
        found = None
        for name, device in self._load().items():
            stored = device.get("hash")
            if isinstance(stored, str) and not device.get("revoked") and hmac.compare_digest(stored, probe):
                found = name
        return found


@dataclass
class Job:
    id: str
    device: str
    text: str
    created: float
    state: str = "queued"           # queued, running, done
    reply: str = ""
    notes: list[str] = field(default_factory=list)  # approval hints said during the turn


class DenyingDeviceChannel:
    """Approval channel of the device assistant: every level-2/3 request is denied, with a hint."""

    def __init__(self, say: Callable[[str], None], language: str = "en") -> None:
        self.say = say
        self.language = language

    def ask(self, request, deadline: float) -> bool:
        from .status import clean_text

        try:
            self.say(TEXTS.get(self.language, TEXTS["en"])["approval"].format(tool=clean_text(request.tool_name, 60)))
        except Exception as exc:  # noqa: BLE001 - the denial stands even if the hint fails
            log.warning("device approval hint failed: %s", type(exc).__name__)
        return False


class DeviceChat:
    """Queues device messages and runs one turn at a time on a worker thread."""

    def __init__(self, config: "Config", devices: DeviceStore,
                 runtime_factory: Callable[[Callable[[str], None]], "AssistantRuntime"], *,
                 has_login: Callable[[], bool] | None = None,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.config = config
        self.devices = devices
        self.runtime_factory = runtime_factory
        self.has_login = has_login or (lambda: _has_login(config))
        self.monotonic = monotonic
        self.t = TEXTS.get(config.language, TEXTS["en"])
        self._cond = threading.Condition()
        self._jobs: collections.OrderedDict[str, Job] = collections.OrderedDict()
        self._queue: queue.Queue = queue.Queue(maxsize=MAX_QUEUED)
        self._recent: dict[str, collections.deque[float]] = {}
        self._turns: collections.deque[float] = collections.deque()
        self._runtime: "AssistantRuntime | None" = None
        self._current: Job | None = None
        self._last_turn = monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __repr__(self) -> str:
        return "DeviceChat(<hidden>)"

    # --- requests (HTTP threads) ---

    def submit(self, device: str, text: object) -> tuple[int, dict]:
        """(HTTP status, payload) for a new message."""
        if not isinstance(text, str) or not text.strip():
            return 400, {"error": "text missing"}
        text = text.strip()[:MAX_TEXT]
        if PIN_LIKE.fullmatch(text) or _is_known_pin(self.config, text):
            return 200, {"reply": self.t["pin"]}
        now = self.monotonic()
        with self._cond:
            recent = self._recent.setdefault(device, collections.deque())
            while recent and now - recent[0] >= 60:
                recent.popleft()
            if len(recent) >= MAX_PER_MIN:
                return 429, {"error": "too many messages, wait a minute"}
            recent.append(now)
            self._expire(now)
            if len(self._jobs) >= MAX_JOBS:
                return 429, {"error": "too many open replies, fetch them first"}
            job = Job(secrets.token_urlsafe(16), device, text, now)
            try:
                self._queue.put_nowait(job)
            except queue.Full:
                return 429, {"error": "busy, try again shortly"}
            self._jobs[job.id] = job
        return 202, {"id": job.id, "state": "queued"}

    def result(self, device: str, job_id: str, wait_s: float) -> tuple[int, dict]:
        """(HTTP status, payload) for a job of this device; waits up to wait_s for the reply."""
        if not JOB_ID_RE.fullmatch(job_id or ""):
            return 404, {"error": "not found"}
        deadline = self.monotonic() + max(0.0, min(wait_s, WAIT_S))
        with self._cond:
            while True:
                job = self._jobs.get(job_id)
                if job is None or job.device != device:
                    return 404, {"error": "not found"}  # another device's job looks like a missing one
                if job.state == "done":
                    del self._jobs[job_id]  # picked up once
                    return 200, {"reply": job.reply}
                left = deadline - self.monotonic()
                if left <= 0 or self._stop.is_set():
                    return 202, {"id": job.id, "state": job.state}
                self._cond.wait(min(left, 1.0))

    def _expire(self, now: float) -> None:
        for job_id in [j.id for j in self._jobs.values() if j.state == "done" and now - j.created > JOB_TTL_S]:
            del self._jobs[job_id]

    # --- the worker ---

    def say(self, text: str) -> None:
        """Approval hints during a turn become part of that turn's reply."""
        job = self._current
        if job is not None:
            job.notes.append(text)

    def _finish(self, job: Job, reply: str) -> None:
        with self._cond:
            job.reply = "\n".join([*job.notes, reply]) if job.notes else reply
            job.state = "done"
            self._cond.notify_all()

    def _turn_allowed(self, runtime: "AssistantRuntime", text: str) -> bool:
        if runtime.router.is_tier1(text):
            return True
        now = self.monotonic()
        while self._turns and now - self._turns[0] >= 3600:
            self._turns.popleft()
        if len(self._turns) >= self.config.telegram_max_turns_per_hour:
            return False
        self._turns.append(now)
        return True

    def work_once(self, timeout: float = 1.0) -> bool:
        from .assistant import reply_lines
        from .bridge import BridgeError

        try:
            job = self._queue.get(timeout=timeout)
        except queue.Empty:
            self._maybe_idle_close()
            return False
        if job is None:
            return False
        with self._cond:
            job.state = "running"
        self._current = job
        try:
            try:
                if self._runtime is None:
                    self._runtime = self.runtime_factory(self.say)
            except Exception as exc:  # noqa: BLE001 - told to the device, the service keeps running
                self._finish(job, self.t["unavailable"].format(reason=type(exc).__name__))
                return True
            runtime = self._runtime
            if not runtime.router.is_tier1(job.text) and not self.has_login():
                self._finish(job, self.t["no_login"])
                return True
            if not self._turn_allowed(runtime, job.text):
                self._finish(job, self.t["limit"])
                return True
            try:
                result = runtime.turn(job.text)
            except BridgeError as exc:
                self._finish(job, self.t["error"].format(reason=type(exc).__name__))
                return True
            self._finish(job, "\n".join(reply_lines(result, runtime.parts.config_dir, prefix="",
                                                    local_paths=False)))
            return True
        finally:
            self._current = None
            self._last_turn = self.monotonic()

    def _maybe_idle_close(self) -> None:
        if self._runtime is not None and self.monotonic() - self._last_turn >= IDLE_CLOSE_S:
            self._last_turn = self.monotonic()
            try:
                self._runtime.idle_close()
            except Exception as exc:  # noqa: BLE001
                log.warning("device chat idle close failed: %s", type(exc).__name__)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="device-chat", daemon=True)
        self._thread.start()
        log.info("device chat on: POST /chat for paired phones, tailnet only (approvals never)")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.work_once()
            except Exception as exc:  # noqa: BLE001 - the worker keeps serving
                log.warning("device turn failed: %s", type(exc).__name__)

    def close(self, join_s: float = 5.0) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._thread is not None:
            self._thread.join(join_s)
        if self._runtime is not None:
            try:
                self._runtime.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("device runtime close failed: %s", type(exc).__name__)


def _is_known_pin(config: "Config", text: str) -> bool:
    probe = text.encode("utf-8")
    found = False
    for pin in (config.approval_pin, config.call_pin):
        if pin and hmac.compare_digest(probe, pin.encode("utf-8")):
            found = True
    return found


def _has_login(config: "Config") -> bool:
    if config.access_mode == "api":
        return bool(config.claude_api_key)
    if config.claude_oauth_token:
        return True
    return (config.bridge_paths()[1] / ".credentials.json").is_file()


def build_device_runtime(config: "Config", say: Callable[[str], None]) -> "AssistantRuntime":
    """The device assistant: the chat's pieces, label "device", an approval channel that always
    denies, no tasks, no delegation, no session lists, mail under the remote rules."""
    from .assistant import AssistantError, finish_assistant, prepare_assistant
    from .status import clean_text

    def make_channel(broker: "ApprovalBroker") -> DenyingDeviceChannel:
        return DenyingDeviceChannel(say, config.language)

    parts = prepare_assistant(config, label="device", channel=make_channel,
                              note=lambda line: log.info("device assistant: %s", clean_text(line, 120)),
                              warn=lambda line: log.warning("device assistant: %s", clean_text(line, 120)))
    try:
        return finish_assistant(parts, tasks=None, delegation=False, sessions=None, model=None,
                                local_listings=False, note_source="device", channel="device")
    except AssistantError:
        parts.broker.close()
        raise


def start_device_chat(config: "Config") -> DeviceChat | None:
    """serve: start the device chat when PA_CHAT_DEVICES=on."""
    if not config.chat_devices:
        return None
    chat = DeviceChat(config, DeviceStore(config.state_dir), lambda say: build_device_runtime(config, say))
    chat.start()
    return chat


__all__ = ["DeviceChat", "DeviceError", "DeviceStore", "build_device_runtime", "start_device_chat"]
