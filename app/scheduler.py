from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections import deque
from datetime import datetime, timezone
from typing import Any

from .alerter import Alerter
from .checker import http_check, tcp_check, docker_check, api_custom_check
from .config import WatchdogConfig, effective_alerting
from .config import HttpCheck, TcpCheck, DockerCheck, ApiCustomCheck, AnyCheck
from .models import AlertPayload, CheckResult, CheckState, CheckStatus

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Scheduler:
    def __init__(self, config: WatchdogConfig, db_path: str = "/data/watchdog.db"):
        self.config = config
        self.db_path = db_path
        self.alerter = Alerter(config)
        self._states: dict[str, CheckState] = {}
        self._history: dict[str, deque[CheckResult]] = {}
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._init_db()
        self._init_states()

    def _init_db(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS check_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                check_name TEXT NOT NULL,
                status TEXT NOT NULL,
                latency_ms REAL,
                error TEXT,
                checked_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_check_results_name_time ON check_results(check_name, checked_at)")
        conn.commit()
        conn.close()

    def _init_states(self) -> None:
        retention = self.config.global_.history_retention
        for check in self.config.checks:
            self._states[check.name] = CheckState(name=check.name)
            self._history[check.name] = deque(maxlen=retention)

    def _persist_result(self, result: CheckResult) -> None:
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "INSERT INTO check_results (check_name, status, latency_ms, error, checked_at) VALUES (?, ?, ?, ?, ?)",
                (result.name, result.status.value, result.latency_ms, result.error, result.checked_at.isoformat()),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"[db] Failed to persist result for '{result.name}': {e}")

    def get_history(self, name: str, hours: int = 24) -> list[CheckResult]:
        try:
            conn = sqlite3.connect(self.db_path)
            cutoff = _utcnow().isoformat()
            rows = conn.execute(
                """
                SELECT status, latency_ms, error, checked_at FROM check_results
                WHERE check_name = ?
                  AND checked_at >= datetime('now', ?)
                ORDER BY checked_at ASC
                """,
                (name, f"-{hours} hours"),
            ).fetchall()
            conn.close()
            results = []
            for row in rows:
                results.append(CheckResult(
                    name=name,
                    status=CheckStatus(row[0]),
                    latency_ms=row[1],
                    error=row[2],
                    checked_at=datetime.fromisoformat(row[3]),
                ))
            return results
        except Exception as e:
            logger.error(f"[db] History query failed: {e}")
            return []

    def compute_uptime_24h(self, name: str) -> float | None:
        results = self.get_history(name, hours=24)
        if not results:
            return None
        up = sum(1 for r in results if r.status == CheckStatus.UP)
        return round(up / len(results) * 100, 2)

    async def _run_check(self, check: AnyCheck) -> CheckResult:
        interval = check.interval or self.config.global_.default_interval
        timeout = check.timeout or self.config.global_.default_timeout

        if isinstance(check, HttpCheck):
            return await http_check(
                url=check.url,
                method=check.method,
                expected_status=check.expected_status,
                headers=check.headers,
                body_contains=check.body_contains,
                latency_warn_ms=check.latency_warn_ms,
                timeout=timeout,
                name=check.name,
            )
        elif isinstance(check, TcpCheck):
            return await tcp_check(
                host=check.host,
                port=check.port,
                timeout=timeout,
                name=check.name,
            )
        elif isinstance(check, DockerCheck):
            return await docker_check(
                container_name=check.container_name,
                check_health=check.check_health,
                name=check.name,
            )
        elif isinstance(check, ApiCustomCheck):
            return await api_custom_check(
                url=check.url,
                validations=[v.model_dump() for v in check.validations],
                method=check.method,
                headers=check.headers,
                timeout=timeout,
                name=check.name,
            )
        else:
            return CheckResult(name=check.name, status=CheckStatus.UNKNOWN, error="Unknown check type")

    async def _process_result(self, check: AnyCheck, result: CheckResult) -> None:
        state = self._states[check.name]
        previous_status = state.status

        self._history[check.name].append(result)
        self._persist_result(result)

        # Update state
        if result.status == CheckStatus.UP:
            state.consecutive_failures = 0
            state.consecutive_successes += 1
            state.last_success_at = result.checked_at
            state.last_error = None
            if state.status in (CheckStatus.DOWN, CheckStatus.DEGRADED):
                logger.info(f"[scheduler] '{check.name}' RECOVERED")
        else:
            state.consecutive_failures += 1
            state.consecutive_successes = 0
            state.last_failure_at = result.checked_at
            state.last_error = result.error
            if state.down_since is None or previous_status == CheckStatus.UP:
                state.down_since = result.checked_at
            logger.warning(f"[scheduler] '{check.name}' {result.status.value}: {result.error}")

        state.status = result.status
        state.last_latency_ms = result.latency_ms
        state.uptime_24h = self.compute_uptime_24h(check.name)

        # Alerting
        is_recovery = result.status == CheckStatus.UP and previous_status in (CheckStatus.DOWN, CheckStatus.DEGRADED)
        if is_recovery:
            payload = AlertPayload(
                check_name=check.name,
                status=result.status,
                previous_status=previous_status,
                down_since=state.down_since,
            )
            state.down_since = None
            state.last_alert_at = _utcnow()
            await self.alerter.dispatch(check, state, payload)
        elif result.status != CheckStatus.UP:
            payload = AlertPayload(
                check_name=check.name,
                status=result.status,
                previous_status=previous_status,
                error=result.error,
                down_since=state.down_since,
                consecutive_failures=state.consecutive_failures,
                latency_ms=result.latency_ms,
            )
            await self.alerter.dispatch(check, state, payload)
            if state.consecutive_failures >= effective_alerting(check, self.config).consecutive_failures_before_alert:
                state.last_alert_at = _utcnow()

    async def _check_loop(self, check: AnyCheck) -> None:
        interval = check.interval or self.config.global_.default_interval
        while self._running:
            try:
                result = await self._run_check(check)
                await self._process_result(check, result)
            except Exception as e:
                logger.error(f"[scheduler] Unhandled error in check '{check.name}': {e}")
            await asyncio.sleep(interval)

    async def _daily_summary_loop(self) -> None:
        """Fire daily summary email at configured time."""
        email_cfg = self.config.alerting.channels.email
        if not email_cfg.enabled or not email_cfg.daily_summary_at:
            return

        import pytz
        tz = pytz.timezone(self.config.global_.timezone)

        while self._running:
            now_local = datetime.now(tz)
            target_h, target_m = map(int, email_cfg.daily_summary_at.split(":"))
            next_run = now_local.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
            if next_run <= now_local:
                from datetime import timedelta
                next_run = next_run + timedelta(days=1)
            wait_secs = (next_run - now_local).total_seconds()
            await asyncio.sleep(wait_secs)
            if self._running:
                await self.alerter.send_daily_summary(self._states)

    def start(self) -> None:
        self._running = True
        for check in self.config.checks:
            task = asyncio.create_task(self._check_loop(check), name=f"check:{check.name}")
            self._tasks.append(task)
        summary_task = asyncio.create_task(self._daily_summary_loop(), name="daily_summary")
        self._tasks.append(summary_task)
        logger.info(f"[scheduler] Started {len(self.config.checks)} checks")

    def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        logger.info("[scheduler] Stopped")

    @property
    def states(self) -> dict[str, CheckState]:
        return self._states
