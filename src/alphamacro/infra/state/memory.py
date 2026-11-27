"""In-memory implementations of CacheStore, RateLimiter, and LockManager."""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from datetime import timedelta

from alphamacro.models import (
    CacheEntry,
    Lease,
    RateDecision,
    RateRule,
)
from alphamacro.ports import Clock


class MemoryCacheStore:
    """Process-local CacheStore adapter suitable for tests and single-process dev."""

    def __init__(
        self,
        clock: Clock,
        *,
        prefix: str = "amc:v1:",
        max_entries: int = 2048,
    ) -> None:
        self._clock = clock
        self._prefix = prefix
        self._max_entries = max(1, max_entries)
        self._data: dict[str, tuple[CacheEntry, float]] = {}
        self._lock = asyncio.Lock()

    def _full_key(self, key: str) -> str:
        return key if key.startswith(self._prefix) else f"{self._prefix}{key}"

    async def get(self, key: str) -> CacheEntry | None:
        full_key = self._full_key(key)
        now_epoch = self._clock.now().timestamp()
        async with self._lock:
            item = self._data.get(full_key)
            if item is None:
                return None
            entry, expires_at_epoch = item
            if now_epoch > expires_at_epoch:
                self._data.pop(full_key, None)
                return None
            return entry

    async def put(self, entry: CacheEntry, *, retain_s: int) -> None:
        full_key = self._full_key(entry.key)
        expires_at_epoch = entry.fetched_at.timestamp() + max(0, retain_s)
        stored = entry.model_copy(update={"key": full_key})
        now_epoch = self._clock.now().timestamp()
        async with self._lock:
            self._data.pop(full_key, None)
            self._data[full_key] = (stored, expires_at_epoch)
            if len(self._data) > self._max_entries:
                self._evict(now_epoch)

    def _evict(self, now_epoch: float) -> None:
        expired = [k for k, (_, exp) in self._data.items() if now_epoch > exp]
        for k in expired:
            del self._data[k]
        while len(self._data) > self._max_entries:
            oldest_key = next(iter(self._data))
            del self._data[oldest_key]


class MemoryRateLimiter:
    """In-memory sliding-window RateLimiter backed by an injected Clock."""

    _PRUNE_THRESHOLD = 10_000

    def __init__(self, clock: Clock, *, prefix: str = "amc:v1:") -> None:
        self._clock = clock
        self._prefix = prefix
        self._events: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()

    async def check(self, key: str, rule: RateRule) -> RateDecision:
        now_epoch = self._clock.now().timestamp()
        window_start = now_epoch - rule.window_s
        bucket = f"{self._prefix}rl:{key}"

        async with self._lock:
            events = self._events.setdefault(bucket, deque())
            while events and events[0] <= window_start:
                events.popleft()
            if len(events) >= rule.limit:
                retry_after = max(0.0, rule.window_s - (now_epoch - events[0]))
                return RateDecision(
                    allowed=False,
                    remaining=0,
                    retry_after_s=retry_after,
                )
            events.append(now_epoch)
            remaining = max(0, rule.limit - len(events))
            if len(self._events) > self._PRUNE_THRESHOLD:
                self._prune(now_epoch, rule.window_s)
            return RateDecision(
                allowed=True,
                remaining=remaining,
                retry_after_s=0.0,
            )

    def _prune(self, now_epoch: float, window_s: int) -> None:
        stale = [
            k
            for k, q in self._events.items()
            if not q or (now_epoch - q[-1]) >= window_s
        ]
        for k in stale:
            del self._events[k]


class MemoryLockManager:
    """In-memory single-flight and leader lease manager."""

    def __init__(self, clock: Clock, *, prefix: str = "amc:v1:") -> None:
        self._clock = clock
        self._prefix = prefix
        self._leases: dict[str, Lease] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, name: str, ttl_s: int) -> Lease | None:
        now = self._clock.now()
        full_name = f"{self._prefix}lock:{name}"
        async with self._lock:
            existing = self._leases.get(full_name)
            if existing is not None and existing.expires_at > now:
                return None
            lease = Lease(
                name=name,
                owner=uuid.uuid4().hex,
                expires_at=now + timedelta(seconds=ttl_s),
            )
            self._leases[full_name] = lease
            return lease

    async def release(self, lease: Lease) -> None:
        full_name = f"{self._prefix}lock:{lease.name}"
        async with self._lock:
            existing = self._leases.get(full_name)
            if existing is not None and existing.owner == lease.owner:
                del self._leases[full_name]
