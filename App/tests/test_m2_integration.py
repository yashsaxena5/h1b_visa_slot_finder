"""
Integration tests for M2 - state detection, error handling, and the state
machine's full run loop.

BrowserLauncher connects to Chrome over CDP rather than launching it (see
app/browser/launcher.py); the `cdp_browser_launcher` fixture stands in a
throwaway Chrome to play that role so these still run unattended. They
never hit the live portal or a real network endpoint either: navigation
is stubbed to load a local fixture instead of the real
usvisascheduling.com, and error classification is tested against
directly-raised exceptions rather than a real (flaky, slow) network
failure. Repeatedly driving an automated browser against the real
WAF-protected portal from a test suite is exactly the bot-detection/
rate-limit risk this project's architecture exists to avoid.
"""
import pytest

from app.detection import ErrorHandler
from app.monitor.availability import AvailabilityChecker
from app.monitor.state_machine import StateMachine
from tests.conftest import load_fixture, StubAlertManager


@pytest.mark.asyncio
async def test_state_detection(cdp_browser_launcher):
    """Test state detection against a local fixture."""
    launcher = cdp_browser_launcher
    await launcher.launch()
    session = launcher.session

    page = await session.get_page()
    await load_fixture(page, "dashboard.html")

    state_info = await session.detect_page_state()

    assert 'state' in state_info
    assert 'confidence' in state_info
    assert state_info['state'] == 'dashboard'


@pytest.mark.asyncio
async def test_error_handler():
    """Test error classification against directly-raised exceptions (no network)."""
    handler = ErrorHandler()

    category = handler.classify_error(TimeoutError("operation timed out"))
    assert category.value == 'timeout'

    category = handler.classify_error(ConnectionRefusedError("Connection refused"))
    assert category.value == 'network'


@pytest.mark.asyncio
async def test_session_expiry_detection(cdp_browser_launcher):
    """Test session expiry detection against a local fixture."""
    launcher = cdp_browser_launcher
    await launcher.launch()
    session = launcher.session

    page = await session.get_page()
    await load_fixture(page, "dashboard.html")

    is_valid = await session.is_session_valid()
    health = session.get_session_health()

    assert isinstance(is_valid, bool)
    assert is_valid is True
    assert health['check_count'] >= 1


@pytest.mark.asyncio
async def test_state_machine(cdp_browser_launcher, monkeypatch, tmp_path):
    """
    Test the state machine's full run loop against a real (CDP-connected)
    browser, with navigation stubbed to a local fixture instead of the
    live portal.
    """
    launcher = cdp_browser_launcher
    await launcher.launch()
    session = launcher.session

    async def fake_navigate_to_portal():
        page = await session.get_page()
        await load_fixture(page, "dashboard.html")
        return True

    monkeypatch.setattr(session, "navigate_to_portal", fake_navigate_to_portal)

    checker = AvailabilityChecker(fingerprint_storage_file=str(tmp_path / "fingerprints.json"))
    sm = StateMachine(availability_checker=checker, alert_manager=StubAlertManager())
    await sm.initialize(launcher)

    await sm.run(max_iterations=3)

    summary = sm.get_state_summary()

    assert summary['loop_count'] > 0
    assert summary['current_state'] is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--capture=no"])
