"""
Shared pytest fixtures for offline tests - these load local mock-HTML
fixtures via page.set_content() and never touch the live portal.
"""
import asyncio
import socket
from pathlib import Path

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from app.browser.launcher import BrowserLauncher

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def event_loop():
    """
    Session-scoped event loop so the session-scoped `browser` fixture
    below (one Chromium launch shared across all tests, not one per
    test) is allowed under pytest-asyncio's default function-scoped loop.
    """
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session")
async def browser():
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        yield b
        await b.close()


@pytest_asyncio.fixture
async def page(browser):
    p = await browser.new_page()
    yield p
    await p.close()


async def load_fixture(page, name: str):
    """Load a local HTML fixture into the page - no network involved."""
    html = (FIXTURES_DIR / name).read_text()
    await page.set_content(html)


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest_asyncio.fixture
async def cdp_browser_launcher():
    """
    BrowserLauncher now connects over CDP to a Chrome you launch yourself
    (see app/browser/launcher.py) rather than launching one itself, so
    tests need something already listening on a debug port to connect
    to. This stands in for "the user's manually-launched Chrome": a
    throwaway headless Chromium started with its own
    --remote-debugging-port, on a dynamically-chosen free port so it can
    never collide with a real Chrome the user might have open on 9222.
    This exercises the real connect_over_cdp() path without requiring a
    live, pre-launched browser window during automated test runs.
    """
    port = _free_tcp_port()

    async with async_playwright() as p:
        external_chrome = await p.chromium.launch(
            headless=True,
            args=[f"--remote-debugging-port={port}"],
        )
        # Make sure at least one context exists before a second CDP
        # client connects and expects browser.contexts to be non-empty.
        await external_chrome.new_context()

        launcher = BrowserLauncher(cdp_url=f"http://localhost:{port}")
        try:
            yield launcher
        finally:
            await launcher.close()
            await external_chrome.close()


class StubAlertManager:
    """
    Drop-in replacement for AlertManager that records calls instead of
    firing real desktop notifications, sound alarms, or Telegram calls -
    tests must never trigger those side effects.
    """

    def __init__(self):
        self.slot_alerts = []
        self.human_required_notices = []
        self.cycle_summaries = []
        self.acknowledged = False

    async def send_slot_alert(self, slot, fingerprint):
        self.slot_alerts.append((slot, fingerprint))
        return {'telegram': True, 'desktop': True, 'sound': True}

    async def notify_human_required(self, reason):
        self.human_required_notices.append(reason)
        return {'telegram': True, 'desktop': True}

    async def notify_cycle_summary(self, location_statuses):
        self.cycle_summaries.append(location_statuses)
        return True

    def acknowledge(self):
        self.acknowledged = True
