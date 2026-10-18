"""Configuration from environment variables.

Everything is read from the environment so that secrets never need to live in a
file inside the repository. See `.env.example` for the full list.
"""

from __future__ import annotations

import ipaddress
import math
import os
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path


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
    workdir: Path | None = None
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

    @property
    def remote_approvals(self) -> bool:
        """Whether `serve` offers approvals from the phone (notice plus one-time link)."""
        return bool(self.discord_webhook and self.public_approval_url)

    def bridge_paths(self) -> tuple[Path, Path]:
        """Working directory and Claude Code config directory for the assistant."""
        workdir = self.workdir or self.state_dir / "workspace"
        config_dir = self.claude_config_dir or self.state_dir / "claude-config"
        return workdir, config_dir

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
            workdir=_optional_path("PA_WORKDIR"),
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
        if bool(self.discord_webhook) != bool(self.public_approval_url):
            raise ConfigError("set PA_DISCORD_WEBHOOK and PA_PUBLIC_APPROVAL_URL together, or neither")
        if self.remote_approvals:
            validate_pin(self.approval_pin, "PA_APPROVAL_PIN")


def validate_pin(pin: str | None, name: str = "PA_CALL_PIN") -> None:
    if not pin or not pin.isascii() or not pin.isdigit() or len(pin) < 6:
        raise ConfigError(f"{name} must be at least 6 digits")
