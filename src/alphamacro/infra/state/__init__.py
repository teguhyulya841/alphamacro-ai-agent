"""State backend factory returning unified CacheStore, RateLimiter, and LockManager."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from alphamacro.clock import SystemClock
from alphamacro.config import StateSettings
from alphamacro.exceptions import ConfigurationError
from alphamacro.infra.state.memory import (
    MemoryCacheStore,
    MemoryLockManager,
    MemoryRateLimiter,
)
from alphamacro.infra.state.redis import (
    RedisCacheStore,
    RedisLockManager,
    RedisRateLimiter,
)
from alphamacro.infra.state.sqlite import (
    SqliteCacheStore,
    SqliteDatabase,
    SqliteLockManager,
    SqliteRateLimiter,
)
from alphamacro.models import StateBackend
from alphamacro.ports import CacheStore, Clock, LockManager, RateLimiter


@dataclass(frozen=True)
class StateBundle:
    """Bundled state ports injected across API, bot, and worker processes."""

    cache: CacheStore
    limiter: RateLimiter
    locks: LockManager
    close_fn: Callable[[], Awaitable[None]] | None = None

    async def aclose(self) -> None:
        """Close underlying state backend resources (e.g. Redis connection pool)."""
        if self.close_fn is not None:
            await self.close_fn()


def build_state(
    settings: StateSettings,
    *,
    clock: Clock | None = None,
    redis_client: Any | None = None,
) -> StateBundle:
    """Instantiate the configured state adapters behind domain ports."""
    active_clock: Clock = clock if clock is not None else SystemClock()

    if settings.backend is StateBackend.MEMORY:
        return StateBundle(
            cache=MemoryCacheStore(active_clock, prefix=settings.key_prefix),
            limiter=MemoryRateLimiter(active_clock, prefix=settings.key_prefix),
            locks=MemoryLockManager(active_clock, prefix=settings.key_prefix),
        )

    if settings.backend is StateBackend.SQLITE:
        db = SqliteDatabase(settings.sqlite_path, active_clock)
        return StateBundle(
            cache=SqliteCacheStore(
                db,
                active_clock,
                prefix=settings.key_prefix,
                degraded_mode=settings.degraded_mode,
            ),
            limiter=SqliteRateLimiter(
                db,
                active_clock,
                prefix=settings.key_prefix,
                degraded_mode=settings.degraded_mode,
            ),
            locks=SqliteLockManager(
                db,
                active_clock,
                prefix=settings.key_prefix,
                degraded_mode=settings.degraded_mode,
            ),
        )

    if settings.backend is StateBackend.REDIS:
        owns_client = redis_client is None
        client = redis_client
        if client is None:
            if settings.redis_url is None:
                raise ConfigurationError(
                    "STATE_REDIS_URL is required when STATE_BACKEND=redis"
                )
            try:
                import redis.asyncio as aioredis  # noqa: PLC0415
            except ImportError as exc:
                raise ConfigurationError(
                    "Redis state backend requires the 'redis' package: "
                    "pip install 'redis>=5.0'"
                ) from exc
            client = aioredis.from_url(
                settings.redis_url.get_secret_value(),
                decode_responses=True,
            )

        async def _close_redis() -> None:
            if owns_client and hasattr(client, "aclose"):
                await client.aclose()

        return StateBundle(
            cache=RedisCacheStore(
                client,
                active_clock,
                prefix=settings.key_prefix,
                degraded_mode=settings.degraded_mode,
            ),
            limiter=RedisRateLimiter(
                client,
                active_clock,
                prefix=settings.key_prefix,
                degraded_mode=settings.degraded_mode,
            ),
            locks=RedisLockManager(
                client,
                active_clock,
                prefix=settings.key_prefix,
                degraded_mode=settings.degraded_mode,
            ),
            close_fn=_close_redis if owns_client else None,
        )

    raise ConfigurationError(f"Unsupported state backend: '{settings.backend}'.")
