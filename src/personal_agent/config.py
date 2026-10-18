"""Configuration from environment variables.

Everything is read from the environment so that secrets never need to live in a
file inside the repository. See `.env.example` for the full list.
"""

from __future__ import annotations

import os
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


def validate_pin(pin: str | None) -> None:
    if not pin or not pin.isdigit() or len(pin) < 6:
        raise ConfigError("PA_CALL_PIN must be at least 6 digits")
