from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class CheckStatus(str, Enum):
    UP = "up"
    DOWN = "down"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


class OverallStatus(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"


class CheckResult(BaseModel):
    name: str
    status: CheckStatus
    latency_ms: float | None = None
    error: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    checked_at: datetime = Field(default_factory=datetime.utcnow)


class CheckState(BaseModel):
    name: str
    status: CheckStatus = CheckStatus.UNKNOWN
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    last_alert_at: datetime | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    down_since: datetime | None = None
    last_error: str | None = None
    last_latency_ms: float | None = None
    uptime_24h: float | None = None  # percentage


class CheckSummary(BaseModel):
    name: str
    status: CheckStatus
    latency_ms: float | None = None
    uptime_24h: float | None = None
    error: str | None = None
    down_since: datetime | None = None
    consecutive_failures: int = 0
    last_checked: datetime | None = None


class StatusResponse(BaseModel):
    overall: OverallStatus
    checks: list[CheckSummary]
    summary: dict[str, int]
    generated_at: datetime = Field(default_factory=datetime.utcnow)


class HistoryPoint(BaseModel):
    status: CheckStatus
    latency_ms: float | None
    error: str | None
    checked_at: datetime


class HistoryResponse(BaseModel):
    name: str
    hours: int
    points: list[HistoryPoint]
    uptime_pct: float | None


class AlertPayload(BaseModel):
    check_name: str
    status: CheckStatus
    previous_status: CheckStatus
    error: str | None = None
    down_since: datetime | None = None
    consecutive_failures: int = 0
    latency_ms: float | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    triggered_at: datetime = Field(default_factory=datetime.utcnow)
