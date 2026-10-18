"""Configuration from environment variables.

Everything is read from the environment so that secrets never need to live in a
file inside the repository. See `.env.example` for the full list.
"""

from __future__ import annotations

import ipaddress
import math
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from .status import SESSION_SOURCES


class ConfigError(ValueError):
    pass


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if not math.isfinite(value):
        raise ConfigError(f"{name} must be a finite number")
    return value


def _parse_quiet_hours(raw: str | None) -> tuple[int, int] | None:
    """Parse "23-7" into (23, 7). None or empty disables quiet hours."""
    if not raw:
        return None
    try:
        start, end = (int(part) for part in raw.split("-", 1))
    except ValueError as exc:
        raise ConfigError("PA_QUIET_HOURS must look like '23-7'") from exc
    if not (0 <= start <= 23 and 0 <= end <= 23):
        raise ConfigError("PA_QUIET_HOURS hours must be between 0 and 23")
    return start, end


WEBHOOK_HOSTS = ("discord.com", "discordapp.com")


def _parse_webhook(raw: str | None) -> str | None:
    """The Discord webhook URL. Error messages never contain the value."""
    if not raw:
        return None
    try:
        parts = urllib.parse.urlsplit(raw)
        host = parts.hostname
    except ValueError as exc:
        raise ConfigError("PA_DISCORD_WEBHOOK is not a valid URL") from exc
    if parts.scheme != "https":
        raise ConfigError("PA_DISCORD_WEBHOOK must be an https:// URL")
    if host not in WEBHOOK_HOSTS or not parts.path.startswith("/api/webhooks/"):
        raise ConfigError("PA_DISCORD_WEBHOOK must be a Discord webhook URL (https://discord.com/api/webhooks/...)")
    return raw


def _parse_public_approval_url(raw: str | None) -> str | None:
    """Base URL of the approval page as the phone sees it; https is added when missing."""
    if not raw:
        return None
    if raw.lower().startswith("http://"):
        raise ConfigError("PA_PUBLIC_APPROVAL_URL must use https")
    if "://" not in raw:
        raw = f"https://{raw}"
    try:
        parts = urllib.parse.urlsplit(raw)
        host = parts.hostname
    except ValueError as exc:
        raise ConfigError("PA_PUBLIC_APPROVAL_URL is not a valid URL") from exc
    if parts.scheme != "https" or not host or parts.query or parts.fragment or parts.username:
        raise ConfigError("PA_PUBLIC_APPROVAL_URL must look like https://host[/prefix]")
    return raw.rstrip("/")


def _parse_trusted_clients(raw: str | None) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Extra client networks allowed on /escalate and /internal/ (loopback always is).

    Needed when `serve` runs in a container: published ports reach it from the
    bridge gateway (e.g. 172.17.0.1), not from loopback.
    """
    if not raw:
        return ()
    networks = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            network = ipaddress.ip_network(part, strict=False)
        except ValueError as exc:
            raise ConfigError("PA_TRUSTED_CLIENTS must be a comma-separated list of IP networks") from exc
        if network.prefixlen == 0 or not network.is_private:
            raise ConfigError("PA_TRUSTED_CLIENTS may only name private or loopback networks, not all addresses")
        networks.append(network)
    return tuple(networks)


_BRIEF_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
BRIEF_DISCORD_MODES = ("counts", "full")
TASK_DISCORD_MODES = ("off", "counts")
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def _env_bool(name: str, default: bool) -> bool:
    """on/off switch; the error never echoes the value."""
    raw = _env(name)
    if raw is None:
        return default
    value = raw.lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{name} must be 'on' or 'off'")


def _parse_brief_time(raw: str | None) -> tuple[int, int] | None:
    """Parse "07:30" (or "7:30") into (7, 30). None or empty turns the morning brief off."""
    if not raw:
        return None
    match = _BRIEF_TIME.fullmatch(raw)
    if not match:
        raise ConfigError("PA_BRIEF_TIME must look like 07:30")
    return int(match[1]), int(match[2])


def _optional_path(name: str) -> Path | None:
    raw = _env(name)
    return Path(raw).expanduser() if raw else None


def _default_state_dir() -> Path:
    base = _env("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "personal-agent"


@dataclass(frozen=True)
class TwilioConfig:
    account_sid: str
    auth_token: str
    from_number: str


@dataclass(frozen=True)
class Config:
    state_dir: Path
    status_dir: Path
    bind_host: str = "127.0.0.1"
    port: int = 8787
    api_token: str | None = None
    provider: str = "fake"
    call_to: str | None = None
    call_pin: str | None = None
    public_url: str | None = None
    language: str = "en"
    max_calls_per_day: int = 3
    min_call_interval_min: int = 30
    quiet_hours: tuple[int, int] | None = None
    waiting_escalate_min: int = 0
    status_max_age_min: int = 24 * 60
    twilio: TwilioConfig | None = field(default=None, repr=False)
    claude_bin: str = "claude"
    claude_model: str = "sonnet"
    vault_dir: Path | None = None
    vault_context_chars: int = 3000
    core_plugin: Path | None = None  # PA_CORE_PLUGIN_DIR; None means auto-discovery
    core_plugin_off: bool = False    # PA_CORE_PLUGIN_DIR=off
    claude_config_dir: Path | None = None
    claude_oauth_token: str | None = field(default=None, repr=False)
    approval_timeout_s: int = 300
    quota_stop: float = 0.9
    router_small_model: str | None = "haiku"
    router_small_max_chars: int = 160
    discord_webhook: str | None = field(default=None, repr=False)
    public_approval_url: str | None = None
    approval_pin: str | None = field(default=None, repr=False)
    trusted_clients: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()
    brief_time: tuple[int, int] | None = None  # PA_BRIEF_TIME, local (hour, minute); None = off
    brief_grace_min: int = 120
    brief_polish: bool = False
    brief_discord: str = "counts"  # "counts": only counts go to Discord; "full": the escaped text
    tasks_enabled: bool = True       # PA_TASKS
    task_parallel: int = 2           # PA_TASK_PARALLEL, 1..2
    task_model: str | None = None    # PA_TASK_MODEL; None: the chat's model
    task_timeout_s: int = 900        # PA_TASK_TIMEOUT_S
    task_quota_stop: float = 0.8     # PA_TASK_QUOTA_STOP, at most quota_stop
    task_discord: str = "off"        # PA_TASK_DISCORD: off | counts
    task_delegation: bool = False    # PA_TASK_DELEGATION
    task_max_age_h: int = 24         # PA_TASK_MAX_AGE_H: older queued tasks expire
    domain_context_chars: int = 4000  # PA_DOMAIN_CONTEXT_CHARS: domain notes per helper prompt, 0 = none
    sessions_source: str = "hooks"   # PA_SESSIONS_SOURCE: hooks | agents | both (chat `sessions` only)

    @property
    def remote_approvals(self) -> bool:
        """Whether `serve` offers approvals from the phone (notice plus one-time link)."""
        return bool(self.discord_webhook and self.public_approval_url)

    @property
    def vault_path(self) -> Path:
        """The vault: the assistant's Markdown workspace (PA_VAULT_DIR or state_dir/vault)."""
        return self.vault_dir or self.state_dir / "vault"

    def bridge_paths(self) -> tuple[Path, Path]:
        """Working directory (the vault) and Claude Code config directory for the assistant."""
        config_dir = self.claude_config_dir or self.state_dir / "claude-config"
        return self.vault_path, config_dir

    @classmethod
    def from_env(cls) -> "Config":
        state_dir = Path(_env("PA_STATE_DIR") or _default_state_dir()).expanduser()
        status_dir = Path(_env("PA_STATUS_DIR") or state_dir / "sessions").expanduser()
        provider = (_env("PA_PROVIDER", "fake") or "fake").lower()
        if provider not in ("fake", "twilio"):
            raise ConfigError("PA_PROVIDER must be 'fake' or 'twilio'")
        language = (_env("PA_LANGUAGE", "en") or "en").lower()
        if language not in ("en", "de"):
            raise ConfigError("PA_LANGUAGE must be 'en' or 'de'")

        twilio = None
        if provider == "twilio":
            sid = _env("TWILIO_ACCOUNT_SID")
            token = _env("TWILIO_AUTH_TOKEN")
            from_number = _env("TWILIO_FROM")
            if not (sid and token and from_number):
                raise ConfigError(
                    "PA_PROVIDER=twilio needs TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_FROM"
                )
            twilio = TwilioConfig(sid, token, from_number)

        approval_timeout_s = _env_int("PA_APPROVAL_TIMEOUT_S", 300)
        if not 10 <= approval_timeout_s <= 3600:
            raise ConfigError("PA_APPROVAL_TIMEOUT_S must be between 10 and 3600 seconds")
        quota_stop = _env_float("PA_QUOTA_STOP", 0.9)
        if not 0 < quota_stop <= 1:
            raise ConfigError("PA_QUOTA_STOP must be greater than 0 and at most 1 (e.g. 0.9)")
        small_model = _env("PA_ROUTER_SMALL_MODEL", "haiku")
        if small_model is not None and small_model.lower() in ("off", "none", "0"):
            small_model = None
        small_max_chars = _env_int("PA_ROUTER_SMALL_MAX_CHARS", 160)
        if not 20 <= small_max_chars <= 2000:
            raise ConfigError("PA_ROUTER_SMALL_MAX_CHARS must be between 20 and 2000")
        approval_pin = _env("PA_APPROVAL_PIN")
        if approval_pin is not None:
            validate_pin(approval_pin, "PA_APPROVAL_PIN")
        vault_dir = _optional_path("PA_VAULT_DIR")
        legacy_workdir = _optional_path("PA_WORKDIR")  # legacy alias of PA_VAULT_DIR
        if vault_dir and legacy_workdir and vault_dir.resolve() != legacy_workdir.resolve():
            raise ConfigError("set PA_VAULT_DIR or PA_WORKDIR, not both")
        vault_context_chars = _env_int("PA_VAULT_CONTEXT_CHARS", 3000)
        if not 0 <= vault_context_chars <= 12000:
            raise ConfigError("PA_VAULT_CONTEXT_CHARS must be between 0 and 12000 (0 turns note excerpts off)")
        core_raw = _env("PA_CORE_PLUGIN_DIR")
        core_plugin_off = core_raw is not None and core_raw.lower() in ("off", "none", "0")
        core_plugin = None if core_raw is None or core_plugin_off else Path(core_raw).expanduser()
        brief_grace_min = _env_int("PA_BRIEF_GRACE_MIN", 120)
        if not 1 <= brief_grace_min <= 720:
            raise ConfigError("PA_BRIEF_GRACE_MIN must be between 1 and 720 minutes")
        brief_discord = (_env("PA_BRIEF_DISCORD", "counts") or "counts").lower()
        if brief_discord not in BRIEF_DISCORD_MODES:
            raise ConfigError("PA_BRIEF_DISCORD must be 'counts' or 'full'")
        tasks_enabled = _env_bool("PA_TASKS", True)
        task_parallel = _env_int("PA_TASK_PARALLEL", 2)
        if not 1 <= task_parallel <= 2:
            raise ConfigError("PA_TASK_PARALLEL must be 1 or 2")
        task_timeout_s = _env_int("PA_TASK_TIMEOUT_S", 900)
        if not 60 <= task_timeout_s <= 3600:
            raise ConfigError("PA_TASK_TIMEOUT_S must be between 60 and 3600 seconds")
        task_quota_stop = _env_float("PA_TASK_QUOTA_STOP", min(0.8, quota_stop))
        if not 0 < task_quota_stop <= quota_stop:
            raise ConfigError("PA_TASK_QUOTA_STOP must be greater than 0 and at most PA_QUOTA_STOP")
        task_discord = (_env("PA_TASK_DISCORD", "off") or "off").lower()
        if task_discord not in TASK_DISCORD_MODES:
            raise ConfigError("PA_TASK_DISCORD must be 'off' or 'counts'")
        task_max_age_h = _env_int("PA_TASK_MAX_AGE_H", 24)
        if not 1 <= task_max_age_h <= 24 * 30:
            raise ConfigError("PA_TASK_MAX_AGE_H must be between 1 and 720 hours")
        domain_context_chars = _env_int("PA_DOMAIN_CONTEXT_CHARS", 4000)
        if not 0 <= domain_context_chars <= 8000:
            raise ConfigError("PA_DOMAIN_CONTEXT_CHARS must be between 0 and 8000 (0 attaches no domain notes)")
        sessions_source = (_env("PA_SESSIONS_SOURCE", "hooks") or "hooks").lower()
        if sessions_source not in SESSION_SOURCES:
            raise ConfigError("PA_SESSIONS_SOURCE must be 'hooks', 'agents' or 'both'")

        return cls(
            state_dir=state_dir,
            status_dir=status_dir,
            bind_host=_env("PA_BIND_HOST", "127.0.0.1") or "127.0.0.1",
            port=_env_int("PA_PORT", 8787),
            api_token=_env("PA_API_TOKEN"),
            provider=provider,
            call_to=_env("PA_CALL_TO"),
            call_pin=_env("PA_CALL_PIN"),
            public_url=(_env("PA_PUBLIC_URL") or "").rstrip("/") or None,
            language=language,
            max_calls_per_day=_env_int("PA_MAX_CALLS_PER_DAY", 3),
            min_call_interval_min=_env_int("PA_MIN_CALL_INTERVAL_MIN", 30),
            quiet_hours=_parse_quiet_hours(_env("PA_QUIET_HOURS")),
            waiting_escalate_min=_env_int("PA_WAITING_ESCALATE_MIN", 0),
            status_max_age_min=_env_int("PA_STATUS_MAX_AGE_MIN", 24 * 60),
            twilio=twilio,
            claude_bin=_env("PA_CLAUDE_BIN", "claude") or "claude",
            claude_model=_env("PA_CLAUDE_MODEL", "sonnet") or "sonnet",
            vault_dir=vault_dir or legacy_workdir,
            vault_context_chars=vault_context_chars,
            core_plugin=core_plugin,
            core_plugin_off=core_plugin_off,
            claude_config_dir=_optional_path("PA_CLAUDE_CONFIG_DIR"),
            claude_oauth_token=_env("CLAUDE_CODE_OAUTH_TOKEN"),
            approval_timeout_s=approval_timeout_s,
            quota_stop=quota_stop,
            router_small_model=small_model,
            router_small_max_chars=small_max_chars,
            discord_webhook=_parse_webhook(_env("PA_DISCORD_WEBHOOK")),
            public_approval_url=_parse_public_approval_url(_env("PA_PUBLIC_APPROVAL_URL")),
            approval_pin=approval_pin,
            trusted_clients=_parse_trusted_clients(_env("PA_TRUSTED_CLIENTS")),
            brief_time=_parse_brief_time(_env("PA_BRIEF_TIME")),
            brief_grace_min=brief_grace_min,
            brief_polish=(_env("PA_BRIEF_POLISH", "") or "").lower() in ("1", "true", "yes"),
            brief_discord=brief_discord,
            tasks_enabled=tasks_enabled,
            task_parallel=task_parallel,
            task_model=_env("PA_TASK_MODEL"),
            task_timeout_s=task_timeout_s,
            task_quota_stop=task_quota_stop,
            task_discord=task_discord,
            task_delegation=_env_bool("PA_TASK_DELEGATION", False),
            task_max_age_h=task_max_age_h,
            domain_context_chars=domain_context_chars,
            sessions_source=sessions_source,
        )

    def validate_for_serve(self) -> None:
        """Checks that only matter when the HTTP server runs."""
        if not self.api_token or len(self.api_token) < 24:
            raise ConfigError("PA_API_TOKEN must be set and at least 24 characters long")
        if not self.call_to:
            raise ConfigError("PA_CALL_TO must be set (the number to call, E.164)")
        validate_pin(self.call_pin)
        if self.provider == "twilio" and not (self.public_url or "").startswith("https://"):
            raise ConfigError("PA_PUBLIC_URL must be an https:// URL when using Twilio")
        if self.public_approval_url and not self.discord_webhook:
            raise ConfigError("PA_PUBLIC_APPROVAL_URL needs PA_DISCORD_WEBHOOK")
        if self.brief_time is not None and not self.discord_webhook:
            raise ConfigError("PA_BRIEF_TIME needs PA_DISCORD_WEBHOOK")
        if self.remote_approvals:
            validate_pin(self.approval_pin, "PA_APPROVAL_PIN")


def validate_pin(pin: str | None, name: str = "PA_CALL_PIN") -> None:
    if not pin or not pin.isascii() or not pin.isdigit() or len(pin) < 6:
        raise ConfigError(f"{name} must be at least 6 digits")
