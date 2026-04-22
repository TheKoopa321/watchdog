from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timezone

import httpx

from .config import WatchdogConfig, EffectiveAlerting, effective_alerting
from .config import AnyCheck
from .email_sender import send_alert_down, send_alert_recovered
from .models import AlertPayload, CheckState, CheckStatus

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _in_quiet_hours(quiet_hours: list, local_now: datetime) -> tuple[bool, str]:
    """Check if current local time falls in any quiet window."""
    current_t = local_now.time().replace(second=0, microsecond=0)
    for qh in quiet_hours:
        try:
            start = time.fromisoformat(qh.start)
            end = time.fromisoformat(qh.end)
            if start <= end:
                if start <= current_t <= end:
                    return True, qh.reason
            else:
                # Crosses midnight
                if current_t >= start or current_t <= end:
                    return True, qh.reason
        except Exception:
            continue
    return False, ""


def _should_alert(
    state: CheckState,
    eff: EffectiveAlerting,
    is_recovery: bool,
) -> bool:
    now = _utcnow()

    if is_recovery:
        return eff.recovery_notify

    if state.consecutive_failures < eff.consecutive_failures_before_alert:
        return False

    if state.last_alert_at is not None:
        elapsed = (now - state.last_alert_at).total_seconds()
        if elapsed < eff.reminder_interval:
            return False

    return True


class Alerter:
    def __init__(self, config: WatchdogConfig):
        self.config = config

    async def dispatch(
        self,
        check: AnyCheck,
        state: CheckState,
        payload: AlertPayload,
    ) -> None:
        eff = effective_alerting(check, self.config)
        is_recovery = payload.status == CheckStatus.UP and payload.previous_status in (
            CheckStatus.DOWN,
            CheckStatus.DEGRADED,
        )

        if not _should_alert(state, eff, is_recovery):
            return

        # Check quiet hours (using UTC for now — TODO: convert to local tz if needed)
        in_quiet, reason = _in_quiet_hours(eff.quiet_hours, _utcnow())
        if in_quiet and not is_recovery:
            logger.info(f"[alerter] Quiet hours active for '{check.name}': {reason}")
            return

        channels = eff.channels
        tasks = []
        for channel in channels:
            if channel == "ntfy":
                tasks.append(self._send_ntfy(check.name, payload, is_recovery))
            elif channel == "email":
                tasks.append(self._send_email(check.name, payload, state, is_recovery))
            elif channel == "global_log":
                tasks.append(self._send_global_log(check.name, payload, is_recovery))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for channel, result in zip(channels, results):
            if isinstance(result, Exception):
                logger.error(f"[alerter] {channel} failed for '{check.name}': {result}")

    async def _send_ntfy(self, name: str, payload: AlertPayload, is_recovery: bool) -> None:
        ntfy = self.config.alerting.channels.ntfy
        if not ntfy.enabled:
            return

        if is_recovery:
            title = f"RECOVERED — {name}"
            message = f"Service rétabli"
            priority = ntfy.recovery_priority
            tags = "white_check_mark"
        else:
            title = f"DOWN — {name}"
            message = payload.error or "Service indisponible"
            if payload.consecutive_failures > 1:
                message += f" ({payload.consecutive_failures} échecs consécutifs)"
            priority = ntfy.priority
            tags = "rotating_light"

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"{ntfy.url}/{ntfy.topic}",
                    content=message,
                    headers={
                        "Title": title,
                        "Priority": priority,
                        "Tags": tags,
                    },
                )
        except Exception as e:
            raise RuntimeError(f"ntfy send failed: {e}") from e

    async def _send_email(
        self,
        name: str,
        payload: AlertPayload,
        state: CheckState,
        is_recovery: bool,
    ) -> None:
        email_cfg = self.config.alerting.channels.email
        if not email_cfg.enabled:
            return

        loop = asyncio.get_event_loop()
        if is_recovery:
            await loop.run_in_executor(
                None,
                send_alert_recovered,
                email_cfg,
                name,
                state.down_since,
            )
        else:
            await loop.run_in_executor(
                None,
                send_alert_down,
                email_cfg,
                name,
                payload.error,
                payload.consecutive_failures,
                state.down_since,
            )

    async def _send_global_log(self, name: str, payload: AlertPayload, is_recovery: bool) -> None:
        gl = self.config.alerting.channels.global_log
        if not gl.enabled or not gl.url:
            return

        level = "info" if is_recovery else "error"
        message = (
            f"[Watchdog] {name} RECOVERED"
            if is_recovery
            else f"[Watchdog] {name} DOWN — {payload.error or 'unavailable'}"
        )

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    gl.url,
                    json={"level": level, "message": message, "source": "watchdog"},
                    headers={"X-Internal-Key": gl.api_key},
                )
        except Exception as e:
            raise RuntimeError(f"global_log send failed: {e}") from e

    async def send_daily_summary(self, states: dict[str, CheckState]) -> None:
        email_cfg = self.config.alerting.channels.email
        if not email_cfg.enabled or not email_cfg.daily_summary_at:
            return

        from .email_sender import send_daily_summary as _send_daily

        summary = [
            {
                "name": name,
                "status": state.status.value,
                "uptime_24h": state.uptime_24h,
                "error": state.last_error,
            }
            for name, state in states.items()
        ]

        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, _send_daily, email_cfg, summary)
            logger.info("[alerter] Daily summary email sent")
        except Exception as e:
            logger.error(f"[alerter] Daily summary failed: {e}")
