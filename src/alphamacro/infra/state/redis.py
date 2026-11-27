"""Redis persistent adapters for CacheStore, RateLimiter, and LockManager."""

from __future__ import annotations

import json
import logging
import math
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from alphamacro.infra.state.memory import (
    MemoryCacheStore,
    MemoryLockManager,
    MemoryRateLimiter,
)
from alphamacro.models import (
    CacheEntry,
    DegradedMode,
    Lease,
    RateDecision,
    RateRule,
)
from alphamacro.ports import Clock

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)

_RATE_LIMIT_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]
local ttl_ms = tonumber(ARGV[5])

local cutoff = now - window
redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff)
local count = redis.call('ZCARD', key)
if count >= limit then
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local oldest_score = now
    if oldest[2] ~= nil then
        oldest_score = tonumber(oldest[2])
    end
    return {0, 0, tostring(oldest_score)}
end
redis.call('ZADD', key, now, member)
redis.call('PEXPIRE', key, ttl_ms)
return {1, limit - (count + 1), tostring(now)}
"""

_UNLOCK_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


def _is_redis_error(exc: BaseException) -> bool:
    if isinstance(exc, OSError):
        return True
    return any(
        cls.__module__.startswith("redis") and "Error" in cls.__name__
        for cls in type(exc).__mro__
    )


class RedisCacheStore:
    """Persistent CacheStore backed by Redis with TTL expiration."""

    def __init__(
        self,
        client: Redis| Any,
        clock: Clock,
        *,
        prefix: str = "amc:v1:",
        degraded_mode: DegradedMode = DegradedMode.FAIL_OPEN,
    ) -> None:
        self._client = client
        self._clock = clock
        self._prefix = prefix
        self._degraded = degraded_mode
        self._fallback = MemoryCacheStore(clock, prefix=prefix)

    def _full_key(self, key: str) -> str:
        return key if key.startswith(self._prefix) else f"{self._prefix}{key}"

    async def get(self, key: str) -> CacheEntry | None:
        full_key = self._full_key(key)
        try:
            raw = await self._client.get(full_key)
            if raw is None:
                return None
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            data = json.loads(raw)
            if int(data.get("schema_version", 1)) != 1:
                logger.warning(
                    "Redis cache schema mismatch for key=%s; treating as miss",
                    full_key,
                )
                return None
            return CacheEntry.model_validate(data)
        except (ValidationError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning(
                "Corrupt Redis cache entry for key=%s (%s); treating as miss",
                full_key,
                exc,
            )
            return None
        except Exception as exc:
            if not _is_redis_error(exc) or self._degraded is DegradedMode.FAIL_CLOSED:
                raise
            logger.warning(
                "Redis cache get failed (%s); falling back to memory cache", exc
            )
            return await self._fallback.get(key)

    async def put(self, entry: CacheEntry, *, retain_s: int) -> None:
        full_key = self._full_key(entry.key)
        stored = entry.model_copy(update={"key": full_key})
        try:
            if retain_s <= 0:
                await self._client.delete(full_key)
                return
            await self._client.set(full_key, stored.model_dump_json(), ex=retain_s)
        except Exception as exc:
            if not _is_redis_error(exc) or self._degraded is DegradedMode.FAIL_CLOSED:
                raise
            logger.warning(
                "Redis cache put failed (%s); writing to memory fallback", exc
            )
            await self._fallback.put(entry, retain_s=retain_s)


class RedisRateLimiter:
    """Atomic sliding-window RateLimiter backed by Redis sorted sets + Lua."""

    def __init__(
        self,
        client: Redis| Any,
        clock: Clock,
        *,
        prefix: str = "amc:v1:",
        degraded_mode: DegradedMode = DegradedMode.FAIL_OPEN,
    ) -> None:
        self._client = client
        self._clock = clock
        self._prefix = prefix
        self._degraded = degraded_mode
        self._fallback = MemoryRateLimiter(clock, prefix=prefix)

    async def check(self, key: str, rule: RateRule) -> RateDecision:
        now_epoch = self._clock.now().timestamp()
        bucket = f"{self._prefix}rl:{key}"
        member = f"{now_epoch}:{uuid.uuid4().hex}"
        ttl_ms = (math.ceil(rule.window_s) + 1) * 1000
        try:
            res = await self._client.eval(
                _RATE_LIMIT_LUA,
                1,
                bucket,
                str(now_epoch),
                str(rule.window_s),
                str(rule.limit),
                member,
                str(ttl_ms),
            )
            allowed = bool(int(res[0]))
            remaining = max(0, int(res[1]))
            oldest_epoch = float(res[2])
            retry_after = (
                0.0
                if allowed
                else max(0.0, rule.window_s - (now_epoch - oldest_epoch))
            )
            return RateDecision(
                allowed=allowed,
                remaining=remaining,
                retry_after_s=retry_after,
            )
        except Exception as exc:
            if not _is_redis_error(exc) or self._degraded is DegradedMode.FAIL_CLOSED:
                raise
            logger.warning(
                "Redis limiter failed (%s); falling back to memory limiter", exc
            )
            return await self._fallback.check(key, rule)


class RedisLockManager:
    """Distributed lease manager backed by Redis SET NX EX and atomic Lua unlock."""

    def __init__(
        self,
        client: Redis| Any,
        clock: Clock,
        *,
        prefix: str = "amc:v1:",
        degraded_mode: DegradedMode = DegradedMode.FAIL_OPEN,
    ) -> None:
        self._client = client
        self._clock = clock
        self._prefix = prefix
        self._degraded = degraded_mode
        self._fallback = MemoryLockManager(clock, prefix=prefix)

    async def acquire(self, name: str, ttl_s: int) -> Lease | None:
        now = self._clock.now()
        full_name = f"{self._prefix}lock:{name}"
        owner = uuid.uuid4().hex
        try:
            acquired = await self._client.set(
                full_name, owner, nx=True, ex=max(1, ttl_s)
            )
            if acquired:
                return Lease(
                    name=name,
                    owner=owner,
                    expires_at=now + timedelta(seconds=ttl_s),
                )
            return None  # noqa: TRY300
        except Exception as exc:
            if not _is_redis_error(exc) or self._degraded is DegradedMode.FAIL_CLOSED:
                raise
            logger.warning("Redis lock acquire failed (%s); using memory lock", exc)
            return await self._fallback.acquire(name, ttl_s)

    async def release(self, lease: Lease) -> None:
        full_name = f"{self._prefix}lock:{lease.name}"
        try:
            await self._client.eval(_UNLOCK_LUA, 1, full_name, lease.owner)
        except Exception as exc:
            if not _is_redis_error(exc) or self._degraded is DegradedMode.FAIL_CLOSED:
                raise
            logger.warning(
                "Redis lock release failed (%s); falling back to memory", exc
            )
            await self._fallback.release(lease)
