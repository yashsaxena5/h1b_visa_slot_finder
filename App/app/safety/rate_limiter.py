"""
Sliding-window rate limiter for outbound calls (e.g. the Telegram API).
"""
import asyncio
import time
from collections import deque


class RateLimiter:
    """
    Allows at most `max_calls` calls within any rolling `period_seconds`
    window. `acquire()` waits for a free slot instead of dropping or
    raising, so a burst of alerts queues briefly rather than losing one.
    """

    def __init__(self, max_calls: int, period_seconds: float = 60.0):
        self.max_calls = max(1, max_calls)
        self.period_seconds = period_seconds
        self._calls: deque = deque()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= self.period_seconds:
                    self._calls.popleft()

                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return

                wait_time = self.period_seconds - (now - self._calls[0])
                await asyncio.sleep(max(wait_time, 0.01))
