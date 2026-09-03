"""
Unit tests for the browser launcher.

BrowserLauncher connects to a Chrome instance you launch yourself over
CDP (see app/browser/launcher.py) - it never launches a browser process
itself. Tests use the `cdp_browser_launcher` fixture (see conftest.py),
which stands in a throwaway headless Chromium with its own debug port to
play that "already-running Chrome" role, so these still run fully
offline/unattended. They never navigate to the live portal either -
repeatedly hitting a WAF-protected site from an automated test suite is
exactly the kind of bot-detection/rate-limit risk this project's
architecture exists to avoid. Session/state-detection behavior is
exercised against local fixtures instead (see test_detection.py).
"""
import pytest
from app.browser.launcher import BrowserLauncher
from tests.conftest import load_fixture

@pytest.mark.asyncio
async def test_browser_launch(cdp_browser_launcher):
    """Test connecting over CDP works and exposes a context/session/page."""
    launcher = cdp_browser_launcher
    context = await launcher.launch()
    assert context is not None
    assert launcher.context is not None
    assert launcher.session is not None

    page = await launcher.session.get_page()
    assert page is not None

@pytest.mark.asyncio
async def test_connect_failure_is_clear_when_nothing_is_listening():
    """
    If Chrome hasn't been started yet (nothing listening on cdp_url),
    connecting must fail clearly rather than hang or silently no-op -
    this is now a real prerequisite step, not something the app can do
    for you.
    """
    port = 1  # reserved/unbound port - nothing will ever be listening here
    launcher = BrowserLauncher(cdp_url=f"http://localhost:{port}")
    with pytest.raises(Exception):
        await launcher.launch()

@pytest.mark.asyncio
async def test_session_detection(cdp_browser_launcher):
    """Test session state detection against a local fixture (no live network)."""
    launcher = cdp_browser_launcher
    await launcher.launch()
    session = launcher.session

    page = await session.get_page()
    await load_fixture(page, "dashboard.html")

    state = await session.detect_page_state()
    assert 'state' in state
    assert 'confidence' in state
    assert state['state'] == 'dashboard'

@pytest.mark.asyncio
async def test_navigate_to_portal_skips_when_already_there(cdp_browser_launcher):
    """
    Regression test: forcing a page.goto() reload when the tab is
    already on the portal domain is exactly what could needlessly
    re-trigger a Cloudflare challenge/403 on a tab that was already fine.
    A route is faked here so getting page.url onto the portal domain
    doesn't require a real network request.
    """
    launcher = cdp_browser_launcher
    await launcher.launch()
    session = launcher.session
    page = await session.get_page()

    async def fake_route(route):
        await route.fulfill(status=200, content_type="text/html", body="<html><body>fake portal</body></html>")

    await page.route("https://www.usvisascheduling.com/**", fake_route)
    await page.goto("https://www.usvisascheduling.com/en-US/ofc-schedule/")

    goto_calls = []
    original_goto = page.goto

    async def spy_goto(*args, **kwargs):
        goto_calls.append(args)
        return await original_goto(*args, **kwargs)

    page.goto = spy_goto

    result = await session.navigate_to_portal()

    assert result is True
    assert goto_calls == []  # must NOT have called page.goto() again

@pytest.mark.asyncio
async def test_get_page_attaches_to_existing_portal_tab(cdp_browser_launcher):
    """
    Regression test: get_page() must attach to a tab you already have
    open and on the portal (simulating "I already logged in manually")
    rather than opening a fresh, blank, off-portal tab - which is exactly
    what previously caused navigate_to_portal() to think it wasn't on the
    portal and force an unnecessary (403-triggering) reload.
    """
    launcher = cdp_browser_launcher
    context = await launcher.launch()

    # Simulate "the user already opened and navigated a tab" - opened
    # directly on the context, before SessionManager ever calls get_page().
    manual_page = await context.new_page()

    async def fake_route(route):
        await route.fulfill(status=200, content_type="text/html", body="<html><body>fake portal</body></html>")

    await manual_page.route("https://www.usvisascheduling.com/**", fake_route)
    await manual_page.goto("https://www.usvisascheduling.com/en-US/ofc-schedule/")

    page = await launcher.session.get_page()

    assert page is manual_page
    assert "usvisascheduling.com" in page.url

@pytest.mark.asyncio
async def test_get_page_falls_back_to_existing_non_portal_tab(cdp_browser_launcher):
    """
    If no tab is on the portal yet but other tabs are open, attach to one
    of them rather than opening a brand new tab.
    """
    launcher = cdp_browser_launcher
    context = await launcher.launch()

    other_page = await context.new_page()
    await other_page.goto("about:blank")

    page = await launcher.session.get_page()

    assert page is other_page

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
