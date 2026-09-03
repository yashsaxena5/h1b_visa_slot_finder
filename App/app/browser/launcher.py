"""
Browser launcher that connects to an externally-launched Chrome instance
over the Chrome DevTools Protocol (CDP), instead of having Playwright
launch (and automation-flag) its own browser process.

You launch Chrome yourself, once, before running this app:

    google-chrome --remote-debugging-port=9222 --user-data-dir=./browser_profile

(macOS: /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome)

Log in there manually, same as before. This app then attaches to that
already-running window via connect_over_cdp() and never launches,
restarts, or closes the browser process itself - it isn't this app's
browser to manage. Launched this way (without --enable-automation, which
is the flag Playwright's own launch()/launch_persistent_context() add on
your behalf), Chrome never sets navigator.webdriver or shows the
"controlled by automated test software" banner in the first place -
there's nothing to hide or spoof, because the automation signal that
used to trip Cloudflare is simply never present.
"""
from typing import Optional
from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Playwright,
)
import logging

from app.config import config
from app.browser.session import SessionManager

logger = logging.getLogger(__name__)


class BrowserLauncher:
    """Connects to (and cleanly disconnects from) an already-running Chrome over CDP."""

    def __init__(self, cdp_url: Optional[str] = None):
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._session_manager: Optional[SessionManager] = None
        self._is_initialized = False
        self._cdp_url = cdp_url or config.get('browser.cdp_url', 'http://localhost:9222')

    async def launch(self, headless: Optional[bool] = None) -> BrowserContext:
        """
        Connect to the Chrome instance already running at `browser.cdp_url`.

        Args:
            headless: unused, accepted only to keep the same call
                signature this method has always had. There's no
                headless mode here - you're connecting to a real,
                visible Chrome you started yourself.

        Returns:
            BrowserContext: the real profile's existing context
                (whatever you're already logged into) - a fresh, empty
                context is only created as a fallback if the browser
                somehow has none open.
        """
        if self._is_initialized:
            logger.warning("Already connected, returning existing context")
            return self._context

        try:
            logger.info(f"Connecting to Chrome over CDP at {self._cdp_url}")
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.connect_over_cdp(self._cdp_url)

            if self._browser.contexts:
                # Reuse the real, already-logged-in context - never spin
                # up a fresh empty one here, that would lose the session.
                self._context = self._browser.contexts[0]
            else:
                self._context = await self._browser.new_context()

            self._session_manager = SessionManager(self._context)
            self._is_initialized = True
            logger.info("Connected to Chrome successfully")

            return self._context

        except Exception as e:
            logger.error(
                f"Could not connect to Chrome at {self._cdp_url}: {e}. "
                f"Start Chrome yourself first, e.g.:\n"
                f"  google-chrome --remote-debugging-port=9222 --user-data-dir=./browser_profile"
            )
            raise

    async def close(self):
        """
        Disconnect from Chrome. This does not close your Chrome window or
        end the process: per Playwright's documented behavior, browser.close()
        on a CDP-connected browser only tears down Playwright's own
        connection to it. That's intentional - this is your real browser,
        not something this app should ever be able to kill out from under you.
        """
        try:
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()

            self._is_initialized = False
            logger.info("Disconnected from Chrome")

        except Exception as e:
            logger.error(f"Error disconnecting from Chrome: {e}")

    @property
    def context(self) -> Optional[BrowserContext]:
        return self._context

    @property
    def session(self) -> Optional[SessionManager]:
        return self._session_manager

    async def __aenter__(self):
        await self.launch()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
