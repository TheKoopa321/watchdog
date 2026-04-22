from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator


# ── YAML env-var substitution ──────────────────────────────────────────────────

_ENV_RE = re.compile(r"\$\{([^}]+)\}")


def _substitute_env(value: Any) -> Any:
    if isinstance(value, str):
        def replace(m: re.Match) -> str:
            var = m.group(1)
            result = os.environ.get(var, "")
            if not result:
                print(f"[config] WARNING: env var ${{{var}}} is not set", file=sys.stderr)
            return result
        return _ENV_RE.sub(replace, value)
    if isinstance(value, dict):
        return {k: _substitute_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env(v) for v in value]
    return value


# ── Pydantic schemas ───────────────────────────────────────────────────────────

class NtfyChannel(BaseModel):
    enabled: bool = True
    url: str = "http://localhost:8009"
    topic: str = "watchdog"
    priority: str = "high"
    recovery_priority: str = "default"


class EmailChannel(BaseModel):
    enabled: bool = False
    smtp_host: str = "127.0.0.1"
    smtp_port: int = 1025
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    from_name: str = "Watchdog"
    from_email: str = ""
    to: str = ""
    daily_summary_at: str | None = None


class GlobalLogChannel(BaseModel):
    enabled: bool = False
    url: str = ""
    api_key: str = ""


class AlertingChannels(BaseModel):
    ntfy: NtfyChannel = Field(default_factory=NtfyChannel)
    email: EmailChannel = Field(default_factory=EmailChannel)
    global_log: GlobalLogChannel = Field(default_factory=GlobalLogChannel)


class QuietHour(BaseModel):
    start: str  # HH:MM
    end: str    # HH:MM
    reason: str = ""


class AlertingDefaults(BaseModel):
    consecutive_failures_before_alert: int = 3
    reminder_interval: int = 1800
    recovery_notify: bool = True
    recovery_cooldown: int = 60
    quiet_hours: list[QuietHour] = Field(default_factory=list)


class AlertingConfig(BaseModel):
    channels: AlertingChannels = Field(default_factory=AlertingChannels)
    defaults: AlertingDefaults = Field(default_factory=AlertingDefaults)


class CheckAlerting(BaseModel):
    consecutive_failures_before_alert: int | None = None
    reminder_interval: int | None = None
    recovery_notify: bool | None = None
    channels: list[Literal["ntfy", "email", "global_log"]] | None = None
    quiet_hours: list[QuietHour] | None = None


class Validation(BaseModel):
    field: str
    operator: Literal["eq", "neq", "contains", "gt", "lt", "exists"]
    value: Any = None
    failure_message: str = "Validation failed"


class BaseCheck(BaseModel):
    name: str
    type: str
    interval: int | None = None
    timeout: int | None = None
    alerting: CheckAlerting = Field(default_factory=CheckAlerting)


class HttpCheck(BaseCheck):
    type: Literal["http"] = "http"
    url: str
    method: str = "GET"
    expected_status: int = 200
    headers: dict[str, str] = Field(default_factory=dict)
    body_contains: str | None = None
    latency_warn_ms: float | None = None


class TcpCheck(BaseCheck):
    type: Literal["tcp"] = "tcp"
    host: str
    port: int


class DockerCheck(BaseCheck):
    type: Literal["docker"] = "docker"
    container_name: str
    check_health: bool = True


class ApiCustomCheck(BaseCheck):
    type: Literal["api_custom"] = "api_custom"
    url: str
    method: str = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    validations: list[Validation] = Field(default_factory=list)


AnyCheck = HttpCheck | TcpCheck | DockerCheck | ApiCustomCheck


class GlobalConfig(BaseModel):
    timezone: str = "America/Toronto"
    internal_api_key: str = ""
    default_interval: int = 60
    default_timeout: int = 10
    history_retention: int = 1440


class WatchdogConfig(BaseModel):
    global_: GlobalConfig = Field(alias="global", default_factory=GlobalConfig)
    alerting: AlertingConfig = Field(default_factory=AlertingConfig)
    checks: list[AnyCheck] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    @model_validator(mode="before")
    @classmethod
    def rename_global(cls, data: Any) -> Any:
        if isinstance(data, dict) and "global" in data:
            data["global_"] = data.pop("global")
        return data

    @model_validator(mode="after")
    def validate_checks_unique_names(self) -> "WatchdogConfig":
        names = [c.name for c in self.checks]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"Duplicate check names: {dupes}")
        return self


def _parse_check(raw: dict) -> AnyCheck:
    check_type = raw.get("type")
    mapping = {
        "http": HttpCheck,
        "tcp": TcpCheck,
        "docker": DockerCheck,
        "api_custom": ApiCustomCheck,
    }
    cls = mapping.get(check_type)
    if cls is None:
        raise ValueError(f"Unknown check type '{check_type}' in check '{raw.get('name')}'")
    return cls(**raw)


def load_config(path: str | Path = "/config/checks.yaml") -> WatchdogConfig:
    config_path = Path(path)
    if not config_path.exists():
        # Fall back to local dev path
        config_path = Path(__file__).parent.parent / "config" / "checks.yaml"

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    raw = _substitute_env(raw)

    # Parse checks individually for better error messages
    raw_checks = raw.pop("checks", [])
    parsed_checks = []
    for i, check_raw in enumerate(raw_checks):
        try:
            parsed_checks.append(_parse_check(check_raw))
        except Exception as e:
            name = check_raw.get("name", f"check[{i}]")
            raise ValueError(f"Invalid check '{name}': {e}") from e

    raw["checks"] = parsed_checks
    return WatchdogConfig.model_validate(raw)


# Effective alerting config for a check (merges global defaults with per-check overrides)
class EffectiveAlerting(BaseModel):
    consecutive_failures_before_alert: int
    reminder_interval: int
    recovery_notify: bool
    recovery_cooldown: int
    channels: list[str]
    quiet_hours: list[QuietHour]


def effective_alerting(check: AnyCheck, config: WatchdogConfig) -> EffectiveAlerting:
    defaults = config.alerting.defaults
    override = check.alerting

    # Active channels: start with all globally-enabled channels, then apply override
    if override.channels is not None:
        channels = list(override.channels)
    else:
        channels = []
        ch = config.alerting.channels
        if ch.ntfy.enabled:
            channels.append("ntfy")
        if ch.email.enabled:
            channels.append("email")
        if ch.global_log.enabled:
            channels.append("global_log")

    return EffectiveAlerting(
        consecutive_failures_before_alert=(
            override.consecutive_failures_before_alert
            if override.consecutive_failures_before_alert is not None
            else defaults.consecutive_failures_before_alert
        ),
        reminder_interval=(
            override.reminder_interval
            if override.reminder_interval is not None
            else defaults.reminder_interval
        ),
        recovery_notify=(
            override.recovery_notify
            if override.recovery_notify is not None
            else defaults.recovery_notify
        ),
        recovery_cooldown=defaults.recovery_cooldown,
        channels=channels,
        quiet_hours=(
            override.quiet_hours
            if override.quiet_hours is not None
            else defaults.quiet_hours
        ),
    )
