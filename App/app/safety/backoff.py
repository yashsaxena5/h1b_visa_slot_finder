"""
Exponential backoff with jitter for transient errors / empty checks.
"""
import random


class BackoffController:
    """
    Tracks a growing delay: each call to next_delay() returns the current
    delay (with jitter applied) and doubles it for next time, up to
    max_backoff. reset() drops it back to the base delay after a
    successful check.
    """

    def __init__(self, base: float = 30, max_backoff: float = 3600, jitter: float = 5):
        self.base = base
        self.max_backoff = max_backoff
        self.jitter = jitter
        self._current = base

    def next_delay(self) -> float:
        delay = min(self._current, self.max_backoff)
        jittered = max(0.0, delay + random.uniform(-self.jitter, self.jitter))
        self._current = min(self._current * 2, self.max_backoff)
        return jittered

    def reset(self):
        self._current = self.base
