from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .checker import http_check, tcp_check, docker_check, api_custom_check, host_metrics_check
from .config import load_config, WatchdogConfig, HttpCheck, TcpCheck, DockerCheck, ApiCustomCheck, HostMetricsCheck
from .models import (
    CheckStatus,
    CheckSummary,
    HistoryPoint,
    HistoryResponse,
    OverallStatus,
    StatusResponse,
)
from .scheduler import Scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


# ── State ──────────────────────────────────────────────────────────────────────

config: WatchdogConfig
scheduler: Scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    global config, scheduler

    config_path = os.environ.get("CONFIG_PATH", "/config/checks.yaml")
    logger.info(f"[main] Loading config from {config_path}")
    try:
        config = load_config(config_path)
    except Exception as e:
        logger.critical(f"[main] Config error: {e}")
        raise SystemExit(1) from e

    logger.info(f"[main] Loaded {len(config.checks)} checks")
    scheduler = Scheduler(config)
    scheduler.start()

    yield

    scheduler.stop()
    logger.info("[main] Shutdown complete")


# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(title="Watchdog", version="1.0.0", lifespan=lifespan)


# ── Auth ───────────────────────────────────────────────────────────────────────

async def verify_api_key(request: Request) -> None:
    expected = os.environ.get("INTERNAL_API_KEY", "")
    if not expected:
        return  # Auth disabled if no key configured
    provided = request.headers.get("X-Internal-Key", "")
    if provided != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "checks_loaded": len(config.checks)}


# ── Status ─────────────────────────────────────────────────────────────────────

def _overall_status(checks: list[CheckSummary]) -> OverallStatus:
    if any(c.status == CheckStatus.DOWN for c in checks):
        return OverallStatus.DOWN
    if any(c.status == CheckStatus.DEGRADED for c in checks):
        return OverallStatus.DEGRADED
    return OverallStatus.OK


@app.get("/api/status", response_model=StatusResponse, dependencies=[Depends(verify_api_key)])
async def get_status():
    states = scheduler.states
    summaries = []
    for check in config.checks:
        state = states.get(check.name)
        if state is None:
            continue
        summaries.append(CheckSummary(
            name=state.name,
            status=state.status,
            latency_ms=state.last_latency_ms,
            uptime_24h=state.uptime_24h,
            error=state.last_error,
            down_since=state.down_since,
            consecutive_failures=state.consecutive_failures,
            last_checked=state.last_success_at or state.last_failure_at,
        ))

    count = {s.value: 0 for s in CheckStatus}
    for s in summaries:
        count[s.status.value] = count.get(s.status.value, 0) + 1

    return StatusResponse(
        overall=_overall_status(summaries),
        checks=summaries,
        summary={"up": count.get("up", 0), "down": count.get("down", 0), "degraded": count.get("degraded", 0), "total": len(summaries)},
    )


@app.get("/api/status/{name}", dependencies=[Depends(verify_api_key)])
async def get_check_status(name: str):
    state = scheduler.states.get(name)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Check '{name}' not found")
    return state.model_dump(mode="json")


# ── History ────────────────────────────────────────────────────────────────────

@app.get("/api/history/{name}", response_model=HistoryResponse, dependencies=[Depends(verify_api_key)])
async def get_history(name: str, hours: int = 24):
    check_names = [c.name for c in config.checks]
    if name not in check_names:
        raise HTTPException(status_code=404, detail=f"Check '{name}' not found")
    results = scheduler.get_history(name, hours=hours)
    uptime = scheduler.compute_uptime_24h(name)
    return HistoryResponse(
        name=name,
        hours=hours,
        points=[
            HistoryPoint(
                status=r.status,
                latency_ms=r.latency_ms,
                error=r.error,
                checked_at=r.checked_at,
            )
            for r in results
        ],
        uptime_pct=uptime,
    )


# ── Manual test ────────────────────────────────────────────────────────────────

@app.post("/api/checks/{name}/test", dependencies=[Depends(verify_api_key)])
async def test_check(name: str):
    check = next((c for c in config.checks if c.name == name), None)
    if check is None:
        raise HTTPException(status_code=404, detail=f"Check '{name}' not found")

    timeout = check.timeout or config.global_.default_timeout
    if isinstance(check, HttpCheck):
        result = await http_check(
            url=check.url, method=check.method, expected_status=check.expected_status,
            headers=check.headers, body_contains=check.body_contains,
            timeout=timeout, name=check.name,
        )
    elif isinstance(check, TcpCheck):
        result = await tcp_check(host=check.host, port=check.port, timeout=timeout, name=check.name)
    elif isinstance(check, DockerCheck):
        result = await docker_check(container_name=check.container_name, check_health=check.check_health, name=check.name)
    elif isinstance(check, ApiCustomCheck):
        result = await api_custom_check(
            url=check.url, validations=[v.model_dump() for v in check.validations],
            method=check.method, headers=check.headers, timeout=timeout, name=check.name,
        )
    elif isinstance(check, HostMetricsCheck):
        result = await host_metrics_check(
            mounts=check.mounts,
            cpu_warn=check.cpu_warn, cpu_crit=check.cpu_crit,
            ram_warn=check.ram_warn, ram_crit=check.ram_crit,
            disk_warn=check.disk_warn, disk_crit=check.disk_crit,
            name=check.name,
        )
    else:
        raise HTTPException(status_code=400, detail="Unknown check type")

    return result.model_dump(mode="json")
