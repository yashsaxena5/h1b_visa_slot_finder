"""
Offline unit tests for AlertManager's routine cycle-summary telemetry.

The real TelegramNotifier send is replaced with an AsyncMock so these
never depend on, or send through, whatever bot_token/chat_id happen to
be configured in the local .env - consistent with StubAlertManager
existing to keep every other test off the real notification layers.
"""
from datetime import datetime, timedelta
from unittest import mock

import pytest

from app.notifications.manager import AlertManager


def _manager_with_mock_telegram() -> AlertManager:
    manager = AlertManager()
    manager._telegram.send_cycle_summary = mock.AsyncMock(return_value=True)
    return manager


@pytest.mark.asyncio
async def test_cycle_summary_sends_on_first_call():
    manager = _manager_with_mock_telegram()

    sent = await manager.notify_cycle_summary({'MUMBAI VAC': 'No slots'})

    assert sent is True
    manager._telegram.send_cycle_summary.assert_awaited_once_with({'MUMBAI VAC': 'No slots'})


@pytest.mark.asyncio
async def test_cycle_summary_throttled_within_interval():
    """
    A second call before cycle_summary_interval_minutes has elapsed must
    not actually send - the poll interval (as low as 30s) is much
    shorter than this heartbeat is meant to be.
    """
    manager = _manager_with_mock_telegram()

    await manager.notify_cycle_summary({'MUMBAI VAC': 'No slots'})
    sent_again = await manager.notify_cycle_summary({'MUMBAI VAC': 'No slots'})

    assert sent_again is False
    manager._telegram.send_cycle_summary.assert_awaited_once()


@pytest.mark.asyncio
async def test_cycle_summary_sends_again_after_interval_elapses():
    manager = _manager_with_mock_telegram()

    await manager.notify_cycle_summary({'MUMBAI VAC': 'No slots'})
    manager._last_cycle_summary_at = (
        datetime.now() - manager._cycle_summary_interval - timedelta(seconds=1)
    )
    sent_again = await manager.notify_cycle_summary({'MUMBAI VAC': 'No slots'})

    assert sent_again is True
    assert manager._telegram.send_cycle_summary.await_count == 2


@pytest.mark.asyncio
async def test_cycle_summary_disabled_when_interval_is_zero():
    """cycle_summary_interval_minutes: 0 must disable this entirely, not send every time."""
    manager = _manager_with_mock_telegram()
    manager._cycle_summary_interval = None

    sent = await manager.notify_cycle_summary({'MUMBAI VAC': 'No slots'})

    assert sent is False
    manager._telegram.send_cycle_summary.assert_not_awaited()


@pytest.mark.asyncio
async def test_cycle_summary_skips_empty_statuses():
    manager = _manager_with_mock_telegram()

    sent = await manager.notify_cycle_summary({})

    assert sent is False
    manager._telegram.send_cycle_summary.assert_not_awaited()
