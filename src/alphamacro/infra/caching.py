"""CachingFeed: stale-while-revalidate, stale-if-error, and single-flight coalescing."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import AwareDatetime, JsonValue

from alphamacro.exceptions import MarketDataError
from alphamacro.models import (
    CacheClass,
    CacheEntry,
    Freshness,
    FreshnessPolicy,
)
from alphamacro.ports import CacheStore, Clock, LockManager

logger = logging.getLogger(__name__)

DEFAULT_POLICIES: dict[CacheClass, FreshnessPolicy] = {
    CacheClass.TICKER: FreshnessPolicy(fresh_ttl_s=20, stale_ttl_s=120),
    CacheClass.MARKETS: FreshnessPolicy(fresh_ttl_s=60, stale_ttl_s=900),
    CacheClass.OHLC_4H: FreshnessPolicy(fresh_ttl_s=300, stale_ttl_s=14_400),
    CacheClass.DAILY_CHART: FreshnessPolicy(fresh_ttl_s=3600, stale_ttl_s=129_600),
}


@dataclass(frozen=True)
class CachedPayload:
    """Result returned by CachingFeed carrying value, freshness, and timestamp."""

    value: JsonValue
    freshness: Freshness
    fetched_at: AwareDatetime
    from_cache: bool


class CachingFeed:
    """Decorator providing single-flight, stale-while-revalidate, and stale-if-error."""

    def __init__(
        self,
        store: CacheStore,
        locks: LockManager,
        clock: Clock,
        *,
        policies: dict[CacheClass, FreshnessPolicy] | None = None,
        lease_ttl_s: int = 30,
    ) -> None:
        self._store = store
        self._locks = locks
        self._clock = clock
        self._policies = policies or DEFAULT_POLICIES
        self._lease_ttl_s = lease_ttl_s
        self._inflight: dict[str, asyncio.Future[CachedPayload]] = {}
        self._bg_tasks: set[asyncio.Task[Any]] = set()

    async def get_or_fetch(
        self,
        *,
        key: str,
        cache_class: CacheClass,
        source: str,
        fetcher: Callable[[], Awaitable[JsonValue]],
        policy: FreshnessPolicy | None = None,
    ) -> CachedPayload:
        """Resolve ``key`` from cache or invoke ``fetcher`` under single-flight."""
        active_policy = policy or self._policies[cache_class]
        now = self._clock.now()
        entry = await self._store.get(key)

        if entry is not None:
            state = entry.freshness(active_policy, now)
            if state is Freshness.FRESH:
                return CachedPayload(
                    value=entry.value,
                    freshness=Freshness.FRESH,
                    fetched_at=entry.fetched_at,
                    from_cache=True,
                )
            if state is Freshness.STALE:
                self._schedule_background_refresh(
                    key=key,
                    cache_class=cache_class,
                    source=source,
                    fetcher=fetcher,
                    policy=active_policy,
                )
                return CachedPayload(
                    value=entry.value,
                    freshness=Freshness.STALE,
                    fetched_at=entry.fetched_at,
                    from_cache=True,
                )

        return await self._coalesced_fetch(
            key=key,
            cache_class=cache_class,
            source=source,
            fetcher=fetcher,
            policy=active_policy,
            stale_fallback=entry,
        )

    def _schedule_background_refresh(
        self,
        *,
        key: str,
        cache_class: CacheClass,
        source: str,
        fetcher: Callable[[], Awaitable[JsonValue]],
        policy: FreshnessPolicy,
    ) -> None:
        if key in self._inflight:
            return
        task = asyncio.create_task(
            self._background_refresh(
                key=key,
                cache_class=cache_class,
                source=source,
                fetcher=fetcher,
                policy=policy,
            )
        )
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _background_refresh(
        self,
        *,
        key: str,
        cache_class: CacheClass,
        source: str,
        fetcher: Callable[[], Awaitable[JsonValue]],
        policy: FreshnessPolicy,
    ) -> None:
        lease = await self._locks.acquire(f"swr:{key}", ttl_s=self._lease_ttl_s)
        if lease is None:
            return
        try:
            value = await fetcher()
            fetched_at = self._clock.now()
            new_entry = CacheEntry(
                key=key,
                cache_class=cache_class,
                value=value,
                fetched_at=fetched_at,
                source=source,
            )
            await self._store.put(new_entry, retain_s=policy.stale_ttl_s)
        except Exception as exc:
            logger.warning("Background cache refresh failed for key=%s: %s", key, exc)
        finally:
            await self._locks.release(lease)

    async def _coalesced_fetch(
        self,
        *,
        key: str,
        cache_class: CacheClass,
        source: str,
        fetcher: Callable[[], Awaitable[JsonValue]],
        policy: FreshnessPolicy,
        stale_fallback: CacheEntry | None,
    ) -> CachedPayload:
        existing_future = self._inflight.get(key)
        if existing_future is not None:
            return await asyncio.shield(existing_future)

        loop = asyncio.get_running_loop()
        future: asyncio.Future[CachedPayload] = loop.create_future()
        self._inflight[key] = future

        try:
            result = await self._execute_fetch_with_lease(
                key=key,
                cache_class=cache_class,
                source=source,
                fetcher=fetcher,
                policy=policy,
                stale_fallback=stale_fallback,
            )
            if not future.done():
                future.set_result(result)
            return result
        except BaseException as exc:
            if not future.done():
                future.set_exception(exc)
            raise
        finally:
            self._inflight.pop(key, None)

    async def _execute_fetch_with_lease(
        self,
        *,
        key: str,
        cache_class: CacheClass,
        source: str,
        fetcher: Callable[[], Awaitable[JsonValue]],
        policy: FreshnessPolicy,
        stale_fallback: CacheEntry | None,
    ) -> CachedPayload:
        lease = await self._locks.acquire(f"fetch:{key}", ttl_s=self._lease_ttl_s)
        try:
            if lease is None:
                entry = await self._store.get(key)
                if entry is not None:
                    state = entry.freshness(policy, self._clock.now())
                    if state is not Freshness.EXPIRED:
                        return CachedPayload(
                            value=entry.value,
                            freshness=state,
                            fetched_at=entry.fetched_at,
                            from_cache=True,
                        )

            value = await fetcher()
            fetched_at = self._clock.now()
            new_entry = CacheEntry(
                key=key,
                cache_class=cache_class,
                value=value,
                fetched_at=fetched_at,
                source=source,
            )
            await self._store.put(new_entry, retain_s=policy.stale_ttl_s)
            return CachedPayload(
                value=value,
                freshness=Freshness.FRESH,
                fetched_at=fetched_at,
                from_cache=False,
            )
        except Exception as exc:
            now = self._clock.now()
            if (
                stale_fallback is not None
                and stale_fallback.age_s(now) <= policy.stale_ttl_s
            ):
                logger.warning(
                    "Upstream failed for key=%s (%s); serving stale (age=%.1fs)",
                    key,
                    type(exc).__name__,
                    stale_fallback.age_s(now),
                )
                return CachedPayload(
                    value=stale_fallback.value,
                    freshness=Freshness.STALE,
                    fetched_at=stale_fallback.fetched_at,
                    from_cache=True,
                )
            if isinstance(exc, MarketDataError):
                raise
            raise MarketDataError(
                f"Upstream data unavailable for '{key}': {type(exc).__name__}"
            ) from exc
        finally:
            if lease is not None:
                await self._locks.release(lease)

    async def aclose(self) -> None:
        """Await any pending stale-while-revalidate background tasks."""
        if not self._bg_tasks:
            return
        tasks = list(self._bg_tasks)
        await asyncio.gather(*tasks, return_exceptions=True)
