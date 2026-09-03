"""
Offline unit tests for StateDetector against local mock-HTML fixtures.
No live network / real portal involved.
"""
import asyncio

import pytest

from app.detection import StateDetector, PortalState
from tests.conftest import load_fixture


@pytest.mark.asyncio
async def test_detects_dashboard(page):
    await load_fixture(page, "dashboard.html")
    detector = StateDetector()
    result = await detector.detect(page, force_refresh=True)
    assert result.state == PortalState.DASHBOARD


@pytest.mark.asyncio
async def test_detects_captcha(page):
    await load_fixture(page, "captcha.html")
    detector = StateDetector()
    result = await detector.detect(page, force_refresh=True)
    assert result.state == PortalState.CAPTCHA
    assert result.confidence > 0.7


@pytest.mark.asyncio
async def test_detects_cloudflare_challenge_as_captcha(page):
    """
    Regression test: Cloudflare's actual interstitial ("Just a moment..."
    title + "Performing security verification..." + a cf-turnstile widget)
    must be recognized as CAPTCHA, not fall through to UNKNOWN. The real
    Turnstile checkbox text lives in a cross-origin iframe invisible to
    page.content(), so this relies on the title check and DOM/widget
    fallback, not just body-text pattern matching.
    """
    await load_fixture(page, "cloudflare_challenge.html")
    detector = StateDetector()
    result = await detector.detect(page, force_refresh=True)
    assert result.state == PortalState.CAPTCHA
    assert result.confidence > 0.7


@pytest.mark.asyncio
async def test_hidden_403_in_script_is_not_mistaken_for_access_denied(page):
    """
    Regression test for a real production false positive: the old
    "http_status" check scanned raw page.content() HTML for the bare
    substrings "403"/"Forbidden" - which matches an analytics/tracker
    script's config blob just as readily as an actual error page, even
    though the rendered page is completely normal. Detection must only
    ever look at visible text/title, never raw HTML source.
    """
    await load_fixture(page, "homepage_with_hidden_403.html")
    detector = StateDetector()
    result = await detector.detect(page, force_refresh=True)
    assert result.state != PortalState.ACCESS_DENIED
    assert result.state == PortalState.DASHBOARD


@pytest.mark.asyncio
async def test_genuine_403_title_is_detected_as_access_denied(page):
    """A real error page (title IS "403 Forbidden") must still be caught."""
    await load_fixture(page, "access_denied_403.html")
    detector = StateDetector()
    result = await detector.detect(page, force_refresh=True)
    assert result.state == PortalState.ACCESS_DENIED
    assert result.confidence > 0.7


@pytest.mark.asyncio
async def test_detects_real_portal_access_limitation_page(page):
    """
    Regression test for a real production incident: the portal's own
    "Access limitation ... Prohibited conduct" block page must be
    recognized as ACCESS_DENIED, not fall through as an unrecognized
    state that CHECK_AVAILABILITY would otherwise keep grinding against.
    """
    await load_fixture(page, "access_limitation.html")
    detector = StateDetector()
    result = await detector.detect(page, force_refresh=True)
    assert result.state == PortalState.ACCESS_DENIED
    assert result.confidence > 0.7


@pytest.mark.asyncio
async def test_detects_maintenance(page):
    await load_fixture(page, "maintenance.html")
    detector = StateDetector()
    result = await detector.detect(page, force_refresh=True)
    assert result.state == PortalState.MAINTENANCE


@pytest.mark.asyncio
async def test_detects_session_expired(page):
    await load_fixture(page, "session_expired.html")
    detector = StateDetector()
    result = await detector.detect(page, force_refresh=True)
    assert result.state == PortalState.SESSION_EXPIRED


@pytest.mark.asyncio
async def test_cache_expires_after_ttl(page):
    """
    Regression test for the "caches forever" bug: detect() must return a
    fresh result once the TTL elapses, not the first-ever cached read.
    """
    await load_fixture(page, "dashboard.html")
    detector = StateDetector()
    detector._cache_ttl = 0.05  # shrink for the test

    first = await detector.detect(page)
    assert first.state == PortalState.DASHBOARD

    await load_fixture(page, "captcha.html")

    # Still within TTL - should return the stale cached dashboard result.
    stale = await detector.detect(page)
    assert stale.state == PortalState.DASHBOARD

    await asyncio.sleep(0.1)

    fresh = await detector.detect(page)
    assert fresh.state == PortalState.CAPTCHA
