"""Tests for app.alerter — plan #292, plan #299, plan #324."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.alerter import Alerter, _safe_header, _should_alert
from app.config import (
    AlertingChannels,
    AlertingConfig,
    AlertingDefaults,
    EffectiveAlerting,
    EmailChannel,
    NtfyChannel,
    QuietHour,
    WatchdogConfig,
)
from app.models import AlertPayload, CheckResult, CheckState, CheckStatus


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_config(
    ntfy_enabled: bool = True,
    email_enabled: bool = False,
) -> WatchdogConfig:
    ntfy = NtfyChannel(enabled=ntfy_enabled, url="http://ntfy.local", topic="test")
    email = EmailChannel(enabled=email_enabled, smtp_host="127.0.0.1", smtp_port=1025)
    channels = AlertingChannels(ntfy=ntfy, email=email)
    alerting = AlertingConfig(
        channels=channels,
        defaults=AlertingDefaults(
            consecutive_failures_before_alert=1,
            reminder_interval=0,
            recovery_notify=True,
        ),
    )
    return WatchdogConfig(checks=[], alerting=alerting)


def _make_http_check(name: str):
    """Return a minimal HttpCheck with the given name."""
    from app.config import HttpCheck, CheckAlerting

    return HttpCheck(
        name=name,
        type="http",
        url="http://example.com",
        alerting=CheckAlerting(),
    )


def _make_state(consecutive_failures: int = 1) -> CheckState:
    return CheckState(
        name="test",
        status=CheckStatus.DOWN,
        consecutive_failures=consecutive_failures,
    )


def _make_payload(
    status: CheckStatus = CheckStatus.DOWN,
    previous_status: CheckStatus = CheckStatus.UP,
    consecutive_failures: int = 1,
) -> AlertPayload:
    return AlertPayload(
        check_name="test",
        status=status,
        previous_status=previous_status,
        consecutive_failures=consecutive_failures,
    )


# ── Tests ──────────────────────────────────────────────────────────────────────


def test_ntfy_header_ascii_safe():
    """_safe_header must return 100% ASCII even when input contains accented chars."""
    value = "Relé é — Hébergement"
    result = _safe_header(value)
    # Must not raise when encoded as ASCII
    result.encode("ascii")  # raises if any non-ASCII character survived
    assert isinstance(result, str)
    assert len(result) > 0


def test_ntfy_header_ascii_safe_in_dispatch():
    """A check.name with accents produces ASCII-only Title header (no UnicodeEncodeError)."""
    check_name = "Relé é"
    config = _make_config(ntfy_enabled=True)
    alerter = Alerter(config)
    check = _make_http_check(check_name)
    state = _make_state(consecutive_failures=1)
    payload = _make_payload()

    posted_headers: dict = {}

    async def fake_post(url, *, content=None, headers=None, **kwargs):
        if headers:
            posted_headers.update(headers)
        resp = MagicMock()
        resp.status_code = 200
        return resp

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(side_effect=fake_post)

    with patch("app.alerter.httpx.AsyncClient", return_value=mock_client):
        asyncio.run(alerter.dispatch(check, state, payload))

    assert "Title" in posted_headers, "ntfy was not called"
    # Must be pure ASCII — encode would raise UnicodeEncodeError otherwise
    posted_headers["Title"].encode("ascii")


def _make_eff(recovery_notify: bool = True, recovery_cooldown: int = 60) -> EffectiveAlerting:
    """Build a minimal EffectiveAlerting for _should_alert unit tests."""
    return EffectiveAlerting(
        consecutive_failures_before_alert=1,
        reminder_interval=0,
        recovery_notify=recovery_notify,
        recovery_cooldown=recovery_cooldown,
        channels=["ntfy"],
        quiet_hours=[],
    )


# ── Recovery cooldown tests ────────────────────────────────────────────────────

def test_recovery_cooldown_allows_first_send():
    """First recovery (last_recovery_at=None) is always dispatched."""
    state = CheckState(name="svc", status=CheckStatus.UP, last_recovery_at=None)
    eff = _make_eff(recovery_notify=True, recovery_cooldown=60)
    assert _should_alert(state, eff, is_recovery=True) is True


def test_recovery_cooldown_suppresses_within_window():
    """Recovery within the cooldown window is suppressed (prevents flapping spam)."""
    recent = datetime.now(timezone.utc) - timedelta(seconds=10)  # 10s ago, cooldown=60
    state = CheckState(name="svc", status=CheckStatus.UP, last_recovery_at=recent)
    eff = _make_eff(recovery_notify=True, recovery_cooldown=60)
    assert _should_alert(state, eff, is_recovery=True) is False


def test_recovery_cooldown_reallows_after_expiry():
    """Recovery after the cooldown window has elapsed is dispatched again."""
    old = datetime.now(timezone.utc) - timedelta(seconds=120)  # 2 min ago, cooldown=60
    state = CheckState(name="svc", status=CheckStatus.UP, last_recovery_at=old)
    eff = _make_eff(recovery_notify=True, recovery_cooldown=60)
    assert _should_alert(state, eff, is_recovery=True) is True


def test_recovery_notify_false_blocks_regardless_of_cooldown():
    """recovery_notify=False blocks all recovery alerts even with fresh last_recovery_at."""
    state = CheckState(name="svc", status=CheckStatus.UP, last_recovery_at=None)
    eff = _make_eff(recovery_notify=False, recovery_cooldown=60)
    assert _should_alert(state, eff, is_recovery=True) is False


# ── DOWN/RECOVERED symmetry tests (plan #324) ──────────────────────────────

def _make_scheduler(db_path: str, consecutive_failures_before_alert: int = 1):
    """Build a minimal Scheduler with one 'svc' check."""
    from app.scheduler import Scheduler
    from app.config import HttpCheck, CheckAlerting

    ntfy = NtfyChannel(enabled=True, url="http://ntfy.local", topic="test")
    email = EmailChannel(enabled=False, smtp_host="127.0.0.1", smtp_port=1025)
    channels = AlertingChannels(ntfy=ntfy, email=email)
    alerting = AlertingConfig(
        channels=channels,
        defaults=AlertingDefaults(
            consecutive_failures_before_alert=consecutive_failures_before_alert,
            reminder_interval=0,
            recovery_notify=True,
        ),
    )
    check = HttpCheck(name="svc", type="http", url="http://example.com", alerting=CheckAlerting())
    config = WatchdogConfig(checks=[check], alerting=alerting)
    return Scheduler(config, db_path=db_path)


def test_recovered_not_sent_when_no_down_alert_dispatched(tmp_path):
    """RECOVERED is NOT dispatched when no DOWN alert was actually sent (sub-threshold blip)."""
    from app.scheduler import Scheduler
    from app.config import HttpCheck, CheckAlerting

    scheduler = _make_scheduler(str(tmp_path / "watchdog.db"), consecutive_failures_before_alert=3)
    check = scheduler.config.checks[0]

    dispatch_calls = []

    async def fake_dispatch(c, s, p):
        dispatch_calls.append(p.status.value)
        return False  # Suppressed (sub-threshold) — DOWN not actually sent

    scheduler.alerter.dispatch = fake_dispatch

    async def run():
        # 1 failure — dispatch called but returns False (sub-threshold)
        await scheduler._process_result(check, CheckResult(name="svc", status=CheckStatus.DOWN, error="timeout"))
        # Recovery — should NOT trigger a RECOVERED dispatch
        await scheduler._process_result(check, CheckResult(name="svc", status=CheckStatus.UP))

    asyncio.run(run())

    # dispatch called once for DOWN (returned False), never for RECOVERED
    assert len(dispatch_calls) == 1, f"Expected 1 dispatch call, got {dispatch_calls}"
    assert dispatch_calls[0] == "down"
    assert scheduler._states["svc"].down_alert_sent is False
    assert scheduler._states["svc"].down_since is None


def test_recovered_sent_after_down_alert_dispatched(tmp_path):
    """RECOVERED is dispatched when a DOWN alert was actually sent (threshold reached)."""
    scheduler = _make_scheduler(str(tmp_path / "watchdog.db"), consecutive_failures_before_alert=1)
    check = scheduler.config.checks[0]

    dispatch_calls = []

    async def fake_dispatch(c, s, p):
        dispatch_calls.append(p.status.value)
        return True  # DOWN sent successfully

    scheduler.alerter.dispatch = fake_dispatch

    async def run():
        # 1 failure — threshold=1, dispatch returns True → down_alert_sent=True
        await scheduler._process_result(check, CheckResult(name="svc", status=CheckStatus.DOWN, error="timeout"))
        # Recovery — should trigger RECOVERED dispatch
        await scheduler._process_result(check, CheckResult(name="svc", status=CheckStatus.UP))

    asyncio.run(run())

    assert len(dispatch_calls) == 2, f"Expected 2 dispatch calls, got {dispatch_calls}"
    assert dispatch_calls[0] == "down"
    assert dispatch_calls[1] == "up"
    # State cleaned up after recovery
    assert scheduler._states["svc"].down_alert_sent is False
    assert scheduler._states["svc"].down_since is None


def test_smtp_failure_does_not_block_ntfy():
    """If the email channel raises ConnectionRefusedError, ntfy still succeeds."""
    config = _make_config(ntfy_enabled=True, email_enabled=True)
    alerter = Alerter(config)
    check = _make_http_check("MyService")
    state = _make_state(consecutive_failures=1)
    payload = _make_payload()

    ntfy_called = []

    async def fake_post(url, *, content=None, headers=None, **kwargs):
        ntfy_called.append(url)
        resp = MagicMock()
        resp.status_code = 200
        return resp

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(side_effect=fake_post)

    def raise_connection_refused(*args, **kwargs):
        raise ConnectionRefusedError("SMTP refused")

    with (
        patch("app.alerter.httpx.AsyncClient", return_value=mock_client),
        patch("app.alerter.send_alert_down", side_effect=raise_connection_refused),
    ):
        # dispatch must NOT raise even though email fails
        asyncio.run(alerter.dispatch(check, state, payload))

    assert len(ntfy_called) == 1, "ntfy should have been called exactly once"
