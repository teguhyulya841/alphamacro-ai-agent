"""SQLite (WAL) persistent adapters for CacheStore, RateLimiter, and LockManager."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
from pydantic import ValidationError

from alphamacro.infra.state.memory import (
    MemoryCacheStore,
    MemoryLockManager,
    MemoryRateLimiter,
)
from alphamacro.models import (
    CacheClass,
    CacheEntry,
    DegradedMode,
    Lease,
    RateDecision,
    RateRule,
)
from alphamacro.ports import Clock

logger = logging.getLogger(__name__)

_MIGRATION_0001 = Path(__file__).parent / "migrations" / "0001_init.sql"


class SqliteDatabase:
    """Manages SQLite file initialization, PRAGMAs, and WAL connections."""

    def __init__(self, path: Path, clock: Clock) -> None:
        self.path = path
        self._clock = clock
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def ensure_initialized(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            sql = _MIGRATION_0001.read_text(encoding="utf-8")
            async with self.connect() as db:
                await db.executescript(sql)
                await db.execute(
                    "INSERT OR IGNORE INTO schema_migrations (version, applied_at) "
                    "VALUES (?, ?)",
                    (1, self._clock.now().isoformat()),
                )
                await db.commit()
            self._initialized = True

    def connect(self) -> _SqliteConnectionContext:
        return _SqliteConnectionContext(self.path)


class _SqliteConnectionContext:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    async def __aenter__(self) -> aiosqlite.Connection:
        self._conn = await aiosqlite.connect(
            self._path,
            timeout=10.0,
            isolation_level=None,
        )
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA synchronous=NORMAL;")
        await self._conn.execute("PRAGMA busy_timeout=5000;")
        await self._conn.execute("PRAGMA foreign_keys=ON;")
        return self._conn

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None


class SqliteCacheStore:
    """Persistent CacheStore backed by SQLite WAL."""

    def __init__(
        self,
        db: SqliteDatabase,
        clock: Clock,
        *,
        prefix: str = "amc:v1:",
        degraded_mode: DegradedMode = DegradedMode.FAIL_OPEN,
    ) -> None:
        self._db = db
        self._clock = clock
        self._prefix = prefix
        self._degraded = degraded_mode
        self._fallback = MemoryCacheStore(clock, prefix=prefix)

    def _full_key(self, key: str) -> str:
        return key if key.startswith(self._prefix) else f"{self._prefix}{key}"

    async def get(self, key: str) -> CacheEntry | None:
        full_key = self._full_key(key)
        now_epoch = self._clock.now().timestamp()
        try:
            await self._db.ensure_initialized()
            async with self._db.connect() as conn:
                cursor = await conn.execute(
                    "SELECT cache_class, value_json, fetched_at, source, "
                    "schema_version, expires_at_epoch "
                    "FROM kv_cache WHERE key = ?",
                    (full_key,),
                )
                row = await cursor.fetchone()
                if row is None:
                    return None
                (
                    cache_class_raw,
                    value_json,
                    fetched_at_raw,
                    source,
                    schema_version,
                    expires_at_epoch,
                ) = row
                if now_epoch > float(expires_at_epoch):
                    await conn.execute(
                        "DELETE FROM kv_cache WHERE key = ?", (full_key,)
                    )
                    return None
                if int(schema_version) != 1:
                    logger.warning(
                        "Cache schema mismatch for key=%s; treating as miss",
                        full_key,
                    )
                    return None
                return CacheEntry(
                    key=full_key,
                    cache_class=CacheClass(cache_class_raw),
                    value=json.loads(value_json),
                    fetched_at=datetime.fromisoformat(fetched_at_raw),
                    source=source,
                    schema_version=int(schema_version),
                )
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            logger.warning(
                "Corrupt cache entry for key=%s (%s); treating as miss",
                full_key,
                exc,
            )
            return None
        except (aiosqlite.Error, OSError) as exc:
            if self._degraded is DegradedMode.FAIL_CLOSED:
                raise
            logger.warning(
                "SQLite cache get failed (%s); bypassing persistent cache", exc
            )
            return None

    async def put(self, entry: CacheEntry, *, retain_s: int) -> None:
        full_key = self._full_key(entry.key)
        expires_at_epoch = entry.fetched_at.timestamp() + max(0, retain_s)
        value_json = json.dumps(entry.value, separators=(",", ":"))
        try:
            await self._db.ensure_initialized()
            async with self._db.connect() as conn:
                await conn.execute("BEGIN IMMEDIATE;")
                try:
                    await conn.execute(
                        "INSERT INTO kv_cache "
                        "(key, cache_class, value_json, fetched_at, source, "
                        "schema_version, expires_at_epoch) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET "
                        "cache_class=excluded.cache_class, "
                        "value_json=excluded.value_json, "
                        "fetched_at=excluded.fetched_at, "
                        "source=excluded.source, "
                        "schema_version=excluded.schema_version, "
                        "expires_at_epoch=excluded.expires_at_epoch",
                        (
                            full_key,
                            entry.cache_class.value,
                            value_json,
                            entry.fetched_at.isoformat(),
                            entry.source,
                            entry.schema_version,
                            expires_at_epoch,
                        ),
                    )
                    await conn.execute("COMMIT;")
                except Exception:
                    await conn.execute("ROLLBACK;")
                    raise
        except (aiosqlite.Error, OSError) as exc:
            if self._degraded is DegradedMode.FAIL_CLOSED:
                raise
            logger.warning("SQLite cache put failed (%s); continuing degraded", exc)


class SqliteRateLimiter:
    """Atomic sliding-window RateLimiter using SQLite BEGIN IMMEDIATE transactions."""

    def __init__(
        self,
        db: SqliteDatabase,
        clock: Clock,
        *,
        prefix: str = "amc:v1:",
        degraded_mode: DegradedMode = DegradedMode.FAIL_OPEN,
    ) -> None:
        self._db = db
        self._clock = clock
        self._prefix = prefix
        self._degraded = degraded_mode
        self._fallback = MemoryRateLimiter(clock, prefix=prefix)

    async def check(self, key: str, rule: RateRule) -> RateDecision:
        now_epoch = self._clock.now().timestamp()
        window_start = now_epoch - rule.window_s
        bucket = f"{self._prefix}rl:{key}"
        try:
            await self._db.ensure_initialized()
            async with self._db.connect() as conn:
                await conn.execute("BEGIN IMMEDIATE;")
                try:
                    await conn.execute(
                        "DELETE FROM rate_events "
                        "WHERE bucket_key = ? AND ts_epoch <= ?",
                        (bucket, window_start),
                    )
                    cursor = await conn.execute(
                        "SELECT COUNT(*), MIN(ts_epoch) "
                        "FROM rate_events WHERE bucket_key = ?",
                        (bucket,),
                    )
                    row = await cursor.fetchone()
                    count = int(row[0]) if row and row[0] is not None else 0
                    oldest = float(row[1]) if row and row[1] is not None else now_epoch

                    if count >= rule.limit:
                        await conn.execute("COMMIT;")
                        retry_after = max(0.0, rule.window_s - (now_epoch - oldest))
                        return RateDecision(
                            allowed=False,
                            remaining=0,
                            retry_after_s=retry_after,
                        )

                    await conn.execute(
                        "INSERT INTO rate_events (bucket_key, ts_epoch) VALUES (?, ?)",
                        (bucket, now_epoch),
                    )
                    await conn.execute("COMMIT;")
                    remaining = max(0, rule.limit - (count + 1))
                    return RateDecision(
                        allowed=True,
                        remaining=remaining,
                        retry_after_s=0.0,
                    )
                except Exception:
                    await conn.execute("ROLLBACK;")
                    raise
        except (aiosqlite.Error, OSError) as exc:
            if self._degraded is DegradedMode.FAIL_CLOSED:
                raise
            logger.warning(
                "SQLite limiter failed (%s); falling back to memory limiter", exc
            )
            return await self._fallback.check(key, rule)


class SqliteLockManager:
    """Cross-process lease manager backed by SQLite row locks."""

    def __init__(
        self,
        db: SqliteDatabase,
        clock: Clock,
        *,
        prefix: str = "amc:v1:",
        degraded_mode: DegradedMode = DegradedMode.FAIL_OPEN,
    ) -> None:
        self._db = db
        self._clock = clock
        self._prefix = prefix
        self._degraded = degraded_mode
        self._fallback = MemoryLockManager(clock, prefix=prefix)

    async def acquire(self, name: str, ttl_s: int) -> Lease | None:
        now = self._clock.now()
        now_epoch = now.timestamp()
        expires_at = now + timedelta(seconds=ttl_s)
        expires_epoch = expires_at.timestamp()
        full_name = f"{self._prefix}lock:{name}"
        owner = uuid.uuid4().hex
        try:
            await self._db.ensure_initialized()
            async with self._db.connect() as conn:
                await conn.execute("BEGIN IMMEDIATE;")
                try:
                    await conn.execute(
                        "DELETE FROM leases WHERE name = ? AND expires_at_epoch <= ?",
                        (full_name, now_epoch),
                    )
                    cursor = await conn.execute(
                        "INSERT OR IGNORE INTO leases (name, owner, expires_at_epoch) "
                        "VALUES (?, ?, ?)",
                        (full_name, owner, expires_epoch),
                    )
                    acquired = cursor.rowcount == 1
                    await conn.execute("COMMIT;")
                    if acquired:
                        return Lease(name=name, owner=owner, expires_at=expires_at)
                    return None
                except Exception:
                    await conn.execute("ROLLBACK;")
                    raise
        except (aiosqlite.Error, OSError) as exc:
            if self._degraded is DegradedMode.FAIL_CLOSED:
                raise
            logger.warning("SQLite lock acquire failed (%s); using memory lock", exc)
            return await self._fallback.acquire(name, ttl_s)

    async def release(self, lease: Lease) -> None:
        full_name = f"{self._prefix}lock:{lease.name}"
        try:
            await self._db.ensure_initialized()
            async with self._db.connect() as conn:
                await conn.execute("BEGIN IMMEDIATE;")
                try:
                    await conn.execute(
                        "DELETE FROM leases WHERE name = ? AND owner = ?",
                        (full_name, lease.owner),
                    )
                    await conn.execute("COMMIT;")
                except Exception:
                    await conn.execute("ROLLBACK;")
                    raise
        except (aiosqlite.Error, OSError) as exc:
            if self._degraded is DegradedMode.FAIL_CLOSED:
                raise
            logger.warning(
                "SQLite lock release failed (%s); falling back to memory", exc
            )
            await self._fallback.release(lease)
