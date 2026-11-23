"""State backend factory returning unified CacheStore, RateLimiter, and LockManager."""

from __future__ import annotations

from dataclasses import dataclass

from alphamacro.clock import SystemClock
from alphamacro.config import StateSettings
from alphamacro.exceptions import ConfigurationError
from alphamacro.infra.state.memory import (
    MemoryCacheStore,
    MemoryLockManager,
    MemoryRateLimiter,
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


def build_state(settings: StateSettings, *, clock: Clock | None = None) -> StateBundle:
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

    raise ConfigurationError(
        f"State backend '{settings.backend}' is not yet enabled; "
        "use 'sqlite' (default) or 'memory'."
    )
