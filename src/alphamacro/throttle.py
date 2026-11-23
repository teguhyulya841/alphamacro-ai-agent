"""Rate limiting and duplicate-request protection shared by the bot and the API."""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Hashable


class SlidingWindowRateLimiter:
    """Allow at most ``max_events`` per ``window_seconds`` for each key."""

    _PRUNE_THRESHOLD = 10_000

    def __init__(
        self,
        max_events: int,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_events < 1:
            raise ValueError("max_events must be >= 1")
        self._max = max_events
        self._window = window_seconds
        self._clock = clock
        self._events: dict[Hashable, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: Hashable) -> tuple[bool, float]:
        """Record an attempt. Returns ``(allowed, retry_after_seconds)``."""
        now = self._clock()
        with self._lock:
            events = self._events.setdefault(key, deque())
            while events and now - events[0] >= self._window:
                events.popleft()
            if len(events) >= self._max:
                return False, max(0.0, self._window - (now - events[0]))
            events.append(now)
            if len(self._events) > self._PRUNE_THRESHOLD:
                self._prune(now)
            return True, 0.0

    def _prune(self, now: float) -> None:
        stale = [k for k, q in self._events.items() if not q or now - q[-1] >= self._window]
        for key in stale:
            del self._events[key]


class ActiveUserRegistry:
    """Ensure a user has at most one request in flight."""

    def __init__(self) -> None:
        self._active: set[int] = set()
        self._lock = threading.Lock()

    def try_acquire(self, key: int) -> bool:
        with self._lock:
            if key in self._active:
                return False
            self._active.add(key)
            return True

    def release(self, key: int) -> None:
        with self._lock:
            self._active.discard(key)
