"""
Watchdog supervisor: reconnects to Chrome + rebuilds the state machine
after a crash (e.g. the CDP connection drops), without restart-looping
forever if something is fundamentally broken. This never launches,
re-logs-in to, or closes your browser - it only retries the CDP
connection to whatever is already running at browser.cdp_url.
"""
import asyncio
import logging
import time
from typing import Awaitable, Callable, List

logger = logging.getLogger(__name__)


class Watchdog:
    """
    Runs `session_factory()` (an async callable that connects to Chrome,
    builds a state machine, and runs it to completion/crash) in a loop,
    retrying on unexpected exceptions up to `max_restarts` within
    `window_seconds`, with a short backoff between attempts.

    A clean return (the state machine stopped itself via a kill switch)
    is not a crash and does not trigger a retry.
    """

    def __init__(self, max_restarts: int = 5, window_seconds: float = 3600, restart_backoff: float = 10):
        self.max_restarts = max_restarts
        self.window_seconds = window_seconds
        self.restart_backoff = restart_backoff
        self._restart_times: List[float] = []

    def _record_restart_and_check_limit(self) -> bool:
        """Returns True if another restart is allowed."""
        now = time.monotonic()
        self._restart_times = [t for t in self._restart_times if now - t < self.window_seconds]
        if len(self._restart_times) >= self.max_restarts:
            return False
        self._restart_times.append(now)
        return True

    async def run(self, session_factory: Callable[[], Awaitable[None]]):
        while True:
            try:
                await session_factory()
                logger.info("Session ended cleanly - watchdog exiting.")
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Session crashed: {e}", exc_info=True)
                if not self._record_restart_and_check_limit():
                    logger.critical(
                        f"Watchdog: hit {self.max_restarts} restarts within "
                        f"{self.window_seconds}s - giving up rather than "
                        f"restart-loop. Fix the underlying issue and restart manually."
                    )
                    raise
                logger.warning(
                    f"Watchdog reconnecting in {self.restart_backoff}s "
                    f"(make sure Chrome is still running at the configured cdp_url)..."
                )
                await asyncio.sleep(self.restart_backoff)
