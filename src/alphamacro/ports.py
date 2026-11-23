"""Architectural ports (Protocols) decoupling domain logic from infrastructure."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import AwareDatetime

from alphamacro.models import (
    CacheEntry,
    CoinId,
    Lease,
    RateDecision,
    RateRule,
)


@runtime_checkable
class Clock(Protocol):
    """Provides timezone-aware current timestamps."""

    def now(self) -> AwareDatetime: ...


@runtime_checkable
class CacheStore(Protocol):
    """Persistent or in-memory key-value store for market data snapshots."""

    async def get(self, key: str) -> CacheEntry | None: ...

    async def put(self, entry: CacheEntry, *, retain_s: int) -> None: ...


@runtime_checkable
class RateLimiter(Protocol):
    """Atomic sliding-window rate limiter."""

    async def check(self, key: str, rule: RateRule) -> RateDecision: ...


@runtime_checkable
class LockManager(Protocol):
    """Distributed or cross-process lease manager for single-flight and leader locks."""

    async def acquire(self, name: str, ttl_s: int) -> Lease | None: ...

    async def release(self, lease: Lease) -> None: ...


@runtime_checkable
class Notifier(Protocol):
    """Outbound notification channel (Telegram Bot API, HMAC webhook, etc.)."""

    async def send(self, destination: str, message: str) -> None: ...


@runtime_checkable
class MarketFeed(Protocol):
    """Upstream market data provider port."""

    async def fetch_spot_price(
        self, coin_id: CoinId, *, now: AwareDatetime
    ) -> float | None: ...
