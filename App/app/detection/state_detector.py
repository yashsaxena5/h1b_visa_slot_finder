"""
Advanced portal state detection with multiple detection methods.
"""
from enum import Enum
from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass
from playwright.async_api import Page
import logging
import re
import time

logger = logging.getLogger(__name__)


class PortalState(Enum):
    """All possible portal states."""
    # Normal states
    DASHBOARD = "dashboard"
    APPOINTMENT = "appointment"
    SCHEDULING = "scheduling"
    CONFIRMATION = "confirmation"

    # Problem states
    LOGIN_REQUIRED = "login_required"
    SESSION_EXPIRED = "session_expired"
    CAPTCHA = "captcha"
    ACCESS_DENIED = "access_denied"
    MAINTENANCE = "maintenance"
    RATE_LIMITED = "rate_limited"

    # Error states
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"

    # Unknown
    UNKNOWN = "unknown"


@dataclass
class StateDetectionResult:
    """Result of state detection."""
    state: PortalState
    confidence: float  # 0.0 to 1.0
    details: str
    detection_method: str
    raw_data: Optional[Dict[str, Any]] = None


class StateDetector:
    """
    Comprehensive state detector using multiple detection strategies.

    Text-pattern checks match against the page's *visible* text
    (document.body.innerText), not raw page.content() HTML. Raw HTML
    includes script tags, meta tags, comments, and hidden elements -
    scanning it for generic substrings like "403" or "500" is prone to
    false positives from completely unrelated content (analytics/tracker
    scripts, version strings, embedded JSON, anything that happens to
    contain those characters anywhere in the markup) even though nothing
    about the actual rendered page indicates a problem. DOM structural
    checks (page.query_selector for specific banners/widgets) are
    unaffected by this and still query the live DOM directly.
    """

    def __init__(self):
        # Load configuration
        from app.config import config
        self._patterns = config.get('portal.patterns', {})
        self._selectors = config.get('portal.selectors', {})

        # Priority order for detection (highest priority first)
        self._detection_chain = [
            self._detect_maintenance,
            self._detect_captcha,
            self._detect_access_denied,
            self._detect_session_expired,
            self._detect_login_page,
            self._detect_rate_limited,
            self._detect_server_error,
            self._detect_by_url,
            self._detect_by_title,
            self._detect_by_dom_elements,
        ]

        # Cache for performance
        self._last_result: Optional[StateDetectionResult] = None
        self._last_result_time: float = 0.0
        self._cache_ttl = 5  # seconds

    async def detect(self, page: Page, force_refresh: bool = False) -> StateDetectionResult:
        """
        Detect current page state using multiple strategies.

        Args:
            page: Playwright page object
            force_refresh: Force fresh detection ignoring cache

        Returns:
            StateDetectionResult with detected state and confidence
        """
        if not force_refresh and self._last_result:
            age = time.monotonic() - self._last_result_time
            if age < self._cache_ttl:
                # Cache hit - still within TTL
                return self._last_result

        try:
            # Get page data once for efficiency
            url = page.url
            title = await page.title()
            content = await page.content()
            visible_text = await self._get_visible_text(page)

            # Try each detection method in priority order
            for detector in self._detection_chain:
                result = await detector(page, url, title, content, visible_text)
                if result and result.confidence > 0.7:
                    self._last_result = result
                    self._last_result_time = time.monotonic()
                    logger.debug(f"Detected state: {result.state} (confidence: {result.confidence})")
                    return result

            # If nothing specific found, return unknown
            result = StateDetectionResult(
                state=PortalState.UNKNOWN,
                confidence=0.3,
                details="No specific state detected",
                detection_method="fallback"
            )
            self._last_result = result
            self._last_result_time = time.monotonic()
            return result

        except Exception as e:
            logger.error(f"State detection failed: {e}")
            return StateDetectionResult(
                state=PortalState.UNKNOWN,
                confidence=0.1,
                details=f"Detection error: {str(e)}",
                detection_method="error"
            )

    async def _get_visible_text(self, page: Page) -> str:
        """
        The page's actually-rendered, human-visible text - deliberately
        NOT the raw HTML source, which also contains script/style/meta
        content, comments, and hidden elements that a real user (and a
        human solving a CAPTCHA in the browser) never sees.
        """
        try:
            return await page.evaluate("() => document.body ? document.body.innerText : ''") or ""
        except Exception as e:
            logger.debug(f"Could not read visible text: {e}")
            return ""

    async def _detect_maintenance(
        self, page: Page, url: str, title: str, content: str, visible_text: str
    ) -> Optional[StateDetectionResult]:
        """Detect maintenance page."""
        maintenance_patterns = self._patterns.get('maintenance', [])

        visible_lower = visible_text.lower()
        for pattern in maintenance_patterns:
            if pattern.lower() in visible_lower:
                # Try to find maintenance end time in the visible text
                time_match = re.search(r'(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm))', visible_text)
                details = "Maintenance in progress"
                if time_match:
                    details += f" until {time_match.group(1)}"

                return StateDetectionResult(
                    state=PortalState.MAINTENANCE,
                    confidence=0.95,
                    details=details,
                    detection_method="maintenance_pattern"
                )

        # Check for maintenance banner elements
        try:
            maintenance_banner = await page.query_selector('.maintenance-banner, .system-maintenance, [class*="maintenance"]')
            if maintenance_banner:
                return StateDetectionResult(
                    state=PortalState.MAINTENANCE,
                    confidence=0.90,
                    details="Maintenance banner detected",
                    detection_method="maintenance_element"
                )
        except Exception:
            pass

        return None

    async def _detect_captcha(
        self, page: Page, url: str, title: str, content: str, visible_text: str
    ) -> Optional[StateDetectionResult]:
        """Detect CAPTCHA challenges."""
        captcha_patterns = self._patterns.get('captcha', [])

        visible_lower = visible_text.lower()
        title_lower = title.lower()
        combined = f"{visible_lower} {title_lower}"

        for pattern in captcha_patterns:
            if pattern.lower() in combined:
                # Determine CAPTCHA type
                captcha_type = "Unknown"
                if "cloudflare" in combined or "cf-chl" in combined or "turnstile" in combined:
                    captcha_type = "Cloudflare"
                elif "recaptcha" in combined or "g-recaptcha" in combined:
                    captcha_type = "reCAPTCHA"
                elif "hcaptcha" in combined:
                    captcha_type = "hCaptcha"

                return StateDetectionResult(
                    state=PortalState.CAPTCHA,
                    confidence=0.95,
                    details=f"{captcha_type} CAPTCHA challenge detected",
                    detection_method="captcha_pattern"
                )

        # Cloudflare's classic interstitial title - a very reliable signal
        # that fires even when the actual challenge text/widget lives
        # inside a cross-origin iframe that page.content() can't see into.
        if 'just a moment' in title_lower:
            return StateDetectionResult(
                state=PortalState.CAPTCHA,
                confidence=0.95,
                details="Cloudflare interstitial title detected ('Just a moment...')",
                detection_method="cloudflare_title"
            )

        # Check for CAPTCHA iframe/elements. Cloudflare Turnstile renders
        # its actual checkbox+text inside a cross-origin iframe from
        # challenges.cloudflare.com, so the visible "Verify you are human"
        # copy is invisible to innerText above - only the iframe/
        # container itself is detectable from the parent document.
        try:
            captcha_element = await page.query_selector(
                'iframe[src*="captcha"], iframe[src*="recaptcha"], '
                'iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"], '
                '.cf-turnstile, #challenge-form, #challenge-running, #challenge-stage, '
                '.cf-browser-verification, [class*="turnstile"]'
            )
            if captcha_element:
                return StateDetectionResult(
                    state=PortalState.CAPTCHA,
                    confidence=0.93,
                    details="CAPTCHA/Cloudflare challenge widget detected in DOM",
                    detection_method="captcha_iframe"
                )
        except Exception:
            pass

        return None

    async def _detect_access_denied(
        self, page: Page, url: str, title: str, content: str, visible_text: str
    ) -> Optional[StateDetectionResult]:
        """Detect access denied / blocked pages."""
        denied_patterns = self._patterns.get('access_denied', [])

        visible_lower = visible_text.lower()
        title_lower = title.lower()

        for pattern in denied_patterns:
            if pattern.lower() in visible_lower or pattern.lower() in title_lower:
                return StateDetectionResult(
                    state=PortalState.ACCESS_DENIED,
                    confidence=0.90,
                    details=f"Access denied: {pattern}",
                    detection_method="denied_pattern"
                )

        # A genuine server/WAF error page almost always sets its page
        # TITLE to exactly "403 Forbidden" or similar. Checking for an
        # EXACT title match here - instead of scanning raw HTML for the
        # bare substrings "403"/"Forbidden" - is what actually
        # distinguishes "this IS an error page" from "an unrelated
        # script/tracker/analytics tag somewhere in the markup of an
        # otherwise perfectly normal page happens to contain those
        # characters." The latter is what was happening before:
        # detection_method="http_status" was a raw content.lower() text
        # scan, not an actual HTTP response status check - there was
        # never any real network/response inspection here, so it had no
        # way to "see" background XHR/telemetry failures either way.
        # This method now only ever looks at visible text and the title.
        if title_lower.strip() in ('403 forbidden', 'forbidden', '403', 'access denied'):
            return StateDetectionResult(
                state=PortalState.ACCESS_DENIED,
                confidence=0.9,
                details=f"Access denied - page title is '{title}'",
                detection_method="error_title"
            )

        return None

    async def _detect_session_expired(
        self, page: Page, url: str, title: str, content: str, visible_text: str
    ) -> Optional[StateDetectionResult]:
        """Detect session expiry."""
        expiry_patterns = self._patterns.get('session_expired', [])

        visible_lower = visible_text.lower()
        for pattern in expiry_patterns:
            if pattern.lower() in visible_lower:
                return StateDetectionResult(
                    state=PortalState.SESSION_EXPIRED,
                    confidence=0.92,
                    details=f"Session expired: {pattern}",
                    detection_method="expiry_pattern"
                )

        # Check for specific session timeout elements
        try:
            timeout_msg = await page.query_selector('.session-timeout, .session-expired, [class*="expired"]')
            if timeout_msg:
                return StateDetectionResult(
                    state=PortalState.SESSION_EXPIRED,
                    confidence=0.85,
                    details="Session timeout message displayed",
                    detection_method="expiry_element"
                )
        except Exception:
            pass

        return None

    async def _detect_login_page(
        self, page: Page, url: str, title: str, content: str, visible_text: str
    ) -> Optional[StateDetectionResult]:
        """Detect login page."""
        # Check URL
        if 'login' in url.lower() or 'signin' in url.lower() or 'auth' in url.lower():
            return StateDetectionResult(
                state=PortalState.LOGIN_REQUIRED,
                confidence=0.85,
                details="Login page from URL",
                detection_method="url_pattern"
            )

        # Check title
        if 'login' in title.lower() or 'sign in' in title.lower():
            return StateDetectionResult(
                state=PortalState.LOGIN_REQUIRED,
                confidence=0.80,
                details=f"Login page from title: {title}",
                detection_method="title_pattern"
            )

        # Check for login form elements
        try:
            login_form = await page.query_selector('form[action*="login"], form[action*="signin"], input[type="password"]')
            if login_form:
                # Additional validation - check for username field too
                username_field = await page.query_selector('input[type="email"], input[name*="username"], input[name*="userid"]')
                if username_field:
                    return StateDetectionResult(
                        state=PortalState.LOGIN_REQUIRED,
                        confidence=0.75,
                        details="Login form detected",
                        detection_method="form_elements"
                    )
        except Exception:
            pass

        return None

    async def _detect_rate_limited(
        self, page: Page, url: str, title: str, content: str, visible_text: str
    ) -> Optional[StateDetectionResult]:
        """Detect rate limiting."""
        visible_lower = visible_text.lower()

        # Descriptive phrases are distinctive enough to match as plain
        # substrings; the bare number "429" is not, so it requires word
        # boundaries to avoid matching inside an unrelated longer number.
        phrase_indicators = ['rate limit', 'too many requests', 'slow down', 'try again later']
        for indicator in phrase_indicators:
            if indicator in visible_lower:
                return StateDetectionResult(
                    state=PortalState.RATE_LIMITED,
                    confidence=0.88,
                    details=f"Rate limited: {indicator}",
                    detection_method="rate_limit_pattern"
                )

        if re.search(r'\b429\b', visible_text):
            return StateDetectionResult(
                state=PortalState.RATE_LIMITED,
                confidence=0.88,
                details="Rate limited: 429",
                detection_method="rate_limit_pattern"
            )

        return None

    async def _detect_server_error(
        self, page: Page, url: str, title: str, content: str, visible_text: str
    ) -> Optional[StateDetectionResult]:
        """Detect server errors."""
        visible_lower = visible_text.lower()

        # Descriptive phrases are distinctive enough to match as plain
        # substrings; bare numeric codes are not (e.g. "500" could be a
        # price, a queue position, a z-index printed for debugging), so
        # they require word boundaries.
        phrase_indicators = ['internal server error', 'service unavailable']
        for indicator in phrase_indicators:
            if indicator in visible_lower:
                return StateDetectionResult(
                    state=PortalState.SERVER_ERROR,
                    confidence=0.85,
                    details=f"Server error: {indicator}",
                    detection_method="server_error_pattern"
                )

        for code in ('500', '502', '503', '504'):
            if re.search(rf'\b{code}\b', visible_text):
                return StateDetectionResult(
                    state=PortalState.SERVER_ERROR,
                    confidence=0.85,
                    details=f"Server error: {code}",
                    detection_method="server_error_pattern"
                )

        return None

    async def _detect_by_url(
        self, page: Page, url: str, title: str, content: str, visible_text: str
    ) -> Optional[StateDetectionResult]:
        """Detect state based on URL patterns."""
        url_lower = url.lower()

        # Appointment related URLs
        # Note: these confidences must clear the detect() loop's >0.7
        # cutoff (see below) - previously they scored 0.55-0.70 and could
        # never win, so every normal/healthy page fell through to UNKNOWN
        # and the state machine could never reach CHECK_AVAILABILITY.
        if 'appointment' in url_lower:
            return StateDetectionResult(
                state=PortalState.APPOINTMENT,
                confidence=0.80,
                details="Appointment page from URL",
                detection_method="url_appointment"
            )

        if 'schedule' in url_lower or 'booking' in url_lower:
            return StateDetectionResult(
                state=PortalState.SCHEDULING,
                confidence=0.78,
                details="Scheduling page from URL",
                detection_method="url_scheduling"
            )

        if 'confirm' in url_lower or 'confirmation' in url_lower:
            return StateDetectionResult(
                state=PortalState.CONFIRMATION,
                confidence=0.75,
                details="Confirmation page from URL",
                detection_method="url_confirmation"
            )

        return None

    async def _detect_by_title(
        self, page: Page, url: str, title: str, content: str, visible_text: str
    ) -> Optional[StateDetectionResult]:
        """Detect state based on page title."""
        title_lower = title.lower()

        if 'dashboard' in title_lower or 'home' in title_lower:
            return StateDetectionResult(
                state=PortalState.DASHBOARD,
                confidence=0.78,
                details=f"Dashboard from title: {title}",
                detection_method="title_dashboard"
            )

        return None

    async def _detect_by_dom_elements(
        self, page: Page, url: str, title: str, content: str, visible_text: str
    ) -> Optional[StateDetectionResult]:
        """Detect state based on specific DOM elements."""
        try:
            # Check for dashboard elements
            dashboard_elements = await page.query_selector('.dashboard, .home-page, [class*="dashboard"]')
            if dashboard_elements:
                return StateDetectionResult(
                    state=PortalState.DASHBOARD,
                    confidence=0.75,
                    details="Dashboard elements detected",
                    detection_method="dom_dashboard"
                )

            # Check for appointment elements
            appt_elements = await page.query_selector('.appointment-list, .slot-selection, [class*="appointment"]')
            if appt_elements:
                return StateDetectionResult(
                    state=PortalState.APPOINTMENT,
                    confidence=0.72,
                    details="Appointment elements detected",
                    detection_method="dom_appointment"
                )
        except Exception:
            pass

        return None

    def clear_cache(self):
        """Clear the detection cache."""
        self._last_result = None
        self._last_result_time = 0.0
        logger.debug("Detection cache cleared")
