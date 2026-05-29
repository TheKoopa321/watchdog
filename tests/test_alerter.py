"""Tests for app.alerter — plan #292."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.alerter import Alerter, _safe_header
from app.config import (
    AlertingChannels,
    AlertingConfig,
    AlertingDefaults,
    EmailChannel,
    NtfyChannel,
    WatchdogConfig,
)
from app.models import AlertPayload, CheckState, CheckStatus


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
