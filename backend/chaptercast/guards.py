"""Small in-process guards: a sliding-window rate limiter and a daily character budget.

Both are deliberately simple and single-process. For a multi-instance deployment
they would move to a shared store such as Redis (see docs/ARCHITECTURE.md).
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, date, datetime


class SlidingWindowLimiter:
    """At most ``limit`` events per ``window`` seconds, per key. Memory is bounded."""

    def __init__(
        self,
        limit: int,
        window: float = 60.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10_000,
    ) -> None:
        self._limit = limit
        self._window = window
        self._clock = clock
        self._max_keys = max_keys
        self._events: dict[str, deque[float]] = {}

    def _prune(self, key: str, now: float) -> deque[float]:
        events = self._events.setdefault(key, deque())
        while events and now - events[0] >= self._window:
            events.popleft()
        return events

    def retry_after(self, key: str) -> float:
        """Seconds until ``key`` may act again; 0 if it may act now. Does not consume."""
        now = self._clock()
        events = self._prune(key, now)
        if len(events) < self._limit:
            return 0.0
        return max(0.0, self._window - (now - events[0]))

    def hit(self, key: str) -> None:
        now = self._clock()
        if key not in self._events and len(self._events) >= self._max_keys:
            self._evict(now)
        self._prune(key, now).append(now)

    def allow(self, key: str) -> tuple[bool, float]:
        """Consume one slot if available. Returns ``(allowed, retry_after_seconds)``."""
        wait = self.retry_after(key)
        if wait > 0:
            return False, wait
        self.hit(key)
        return True, 0.0

    def _evict(self, now: float) -> None:
        for key in list(self._events):
            if not self._prune(key, now):
                del self._events[key]
        while len(self._events) >= self._max_keys:
            self._events.pop(next(iter(self._events)))


class DailyBudget:
    """A global character budget that resets at 00:00 UTC.

    It exists so that a public demo (or a bug) cannot burn through the
    ElevenLabs credits behind the API key.
    """

    def __init__(self, limit: int, *, today: Callable[[], date] | None = None) -> None:
        self._limit = limit
        self._today = today or (lambda: datetime.now(UTC).date())
        self._day = self._today()
        self._used = 0

    def _roll(self) -> None:
        today = self._today()
        if today != self._day:
            self._day = today
            self._used = 0

    @property
    def remaining(self) -> int:
        self._roll()
        return max(0, self._limit - self._used)

    def reserve(self, chars: int) -> bool:
        self._roll()
        if self._used + chars > self._limit:
            return False
        self._used += chars
        return True

    def refund(self, chars: int) -> None:
        self._roll()
        self._used = max(0, self._used - chars)
