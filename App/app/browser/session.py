"""
Session management and state detection - Updated with better error handling.
"""
from typing import Optional, Dict, Any
from playwright.async_api import BrowserContext, Page
import logging

from app.detection import StateDetector, SessionExpiryDetector

logger = logging.getLogger(__name__)


class SessionManager:
    """Manages browser session state and detection."""
    
    def __init__(self, context: BrowserContext):
        self._context = context
        self._page: Optional[Page] = None
        self._session_valid = False
        self._current_url = None
        
        # Initialize detectors
        self._state_detector = StateDetector()
        self._session_expiry = SessionExpiryDetector()
        
        # Load config
        from app.config import config
        self._portal_url = config.portal_url
        self._allowed_domains = config.get('portal.allowed_domains', [])

    def _is_on_portal(self, url: str) -> bool:
        """Whether `url` is already on one of the portal's allowed domains."""
        url_lower = (url or '').lower()
        return any(domain.lower() in url_lower for domain in self._allowed_domains)

    async def get_page(self) -> Page:
        """
        Get the page to operate on - attaching to a tab that's already
        open in the connected Chrome, rather than opening a new blank one.

        A freshly-opened tab starts at about:blank, which
        navigate_to_portal() reads as "not on the portal yet" and would
        force a page.goto() - an unnecessary extra request that can
        needlessly re-trigger a Cloudflare challenge on a browser that
        was already logged in and sitting on the portal in another tab.
        """
        if self._page is not None and not self._page.is_closed():
            return self._page

        pages = self._context.pages

        # Prefer a tab that's already on the portal.
        for page in pages:
            if self._is_on_portal(page.url):
                self._page = page
                logger.info(f"Attached to existing portal tab: {page.url}")
                return self._page

        # No portal tab found, but other tabs exist - attach to one of
        # them rather than opening a new (blank, off-portal) tab.
        if pages:
            self._page = pages[0]
            logger.info(f"No portal tab found - attached to existing tab: {self._page.url}")
            return self._page

        # Absolute last resort: no tabs open at all.
        self._page = await self._context.new_page()
        logger.debug("No existing tabs found - created a new page")
        return self._page

    async def navigate_to_portal(self) -> bool:
        """
        Navigate to the portal URL - unless the page is already there.

        You typically log in and navigate manually in the connected
        Chrome tab yourself; forcing an extra page.goto() reload of a
        page that's already open (and already past Cloudflare) is exactly
        the kind of unnecessary request that can needlessly re-trigger a
        403/challenge on a tab that was already fine.
        """
        try:
            page = await self.get_page()
            current_url = page.url

            if self._is_on_portal(current_url):
                logger.info(f"Already on the portal ({current_url}) - skipping navigation")
                self._current_url = current_url
                return True

            await page.goto(
                self._portal_url,
                wait_until='domcontentloaded',
                timeout=30000
            )
            self._current_url = page.url
            logger.info(f"Navigated to portal: {self._current_url}")
            return True
        except Exception as e:
            logger.error(f"Failed to navigate: {e}")
            return False
    
    async def detect_page_state(self, force_refresh: bool = True) -> Dict[str, Any]:
        """
        Detect the current page state using state detector.

        Defaults to force_refresh=True: this method gates safety-critical
        decisions (CAPTCHA/maintenance/session-expiry), so it must never
        silently hand back a stale cached read.
        """
        page = await self.get_page()
        result = await self._state_detector.detect(page, force_refresh=force_refresh)

        return {
            'state': result.state.value,
            'confidence': result.confidence,
            'details': result.details,
            'detection_method': result.detection_method,
            'url': page.url,
        }
    
    async def is_session_valid(self) -> bool:
        """Check if current session is valid."""
        page = await self.get_page()
        result = await self._session_expiry.check_session(page)
        self._session_valid = result['is_valid']
        return self._session_valid
    
    async def wait_for_element(
        self, 
        selector: str, 
        timeout: int = 10000,
        state: str = 'visible'
    ) -> bool:
        """Wait for an element to appear."""
        try:
            page = await self.get_page()
            await page.wait_for_selector(
                selector,
                state=state,
                timeout=timeout
            )
            return True
        except Exception as e:
            logger.debug(f"Element not found: {selector} - {e}")
            return False
    
    async def get_element_text(self, selector: str) -> Optional[str]:
        """Get text content of an element."""
        try:
            page = await self.get_page()
            element = await page.query_selector(selector)
            if element:
                return await element.text_content()
            return None
        except Exception as e:
            logger.debug(f"Failed to get element text: {e}")
            return None
    
    async def take_screenshot(self, name: str = "debug") -> bytes:
        """Take a screenshot of current page."""
        try:
            page = await self.get_page()
            return await page.screenshot(full_page=True)
        except Exception as e:
            logger.error(f"Failed to take screenshot: {e}")
            return b""
    
    async def close_page(self):
        """Close the current page."""
        if self._page and not self._page.is_closed():
            await self._page.close()
            self._page = None
    
    async def refresh_page(self) -> bool:
        """Refresh the current page."""
        try:
            page = await self.get_page()
            await page.reload(wait_until='domcontentloaded')
            return True
        except Exception as e:
            logger.error(f"Failed to refresh page: {e}")
            return False
    
    def get_session_health(self) -> Dict[str, Any]:
        """Get session health information."""
        return self._session_expiry._session_health if self._session_expiry else {}