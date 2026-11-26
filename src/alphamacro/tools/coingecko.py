"""CoinGecko market-data clients (async + legacy sync) and LangChain tool wrappers."""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
import requests
from langchain_core.tools import tool
from pydantic import AwareDatetime
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from alphamacro import __version__
from alphamacro.clock import SystemClock
from alphamacro.constants import MARKET_TOOL_NAME, WIB
from alphamacro.exceptions import CoinNotFoundError, MarketDataError
from alphamacro.infra.caching import DEFAULT_POLICIES, CachedPayload, CachingFeed
from alphamacro.infra.http import create_async_client, get_json_with_retry
from alphamacro.infra.state.memory import MemoryCacheStore, MemoryLockManager
from alphamacro.models import (
    CacheClass,
    CrossValidationReport,
    Freshness,
    FreshnessPolicy,
    Observation,
)
from alphamacro.ports import Clock

if TYPE_CHECKING:
    from alphamacro.tools.venues import MultiVenueValidator

logger = logging.getLogger(__name__)

_COIN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,63}$")


@dataclass(frozen=True)
class ProvenanceData:
    """Data payload paired with cache freshness and fetch timestamps."""

    data: Any
    fetched_at: AwareDatetime
    observed_at: AwareDatetime
    freshness: Freshness
    age_s: float


def normalize_coin_id(coin_id: str) -> str:
    """Validate and normalise a coin id (also prevents URL/path injection)."""
    normalized = (coin_id or "").lower().strip()
    if not _COIN_ID_RE.match(normalized):
        raise MarketDataError(
            f"Coin ID '{coin_id}' tidak valid. Gunakan CoinGecko ID, "
            "mis. bitcoin, ethereum, solana."
        )
    return normalized


def _parse_observed_at(raw_timestamp: Any, fallback: AwareDatetime) -> AwareDatetime:
    if isinstance(raw_timestamp, str) and raw_timestamp.strip():
        try:
            dt = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
        except ValueError:
            return fallback
    return fallback


def _float_or_none(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def build_market_observation(
    snapshot: dict[str, Any],
    *,
    fetched_at: AwareDatetime,
    observed_at: AwareDatetime,
    freshness: Freshness,
    age_s: float,
    cross_validation: CrossValidationReport | None = None,
) -> Observation:
    """Build a structured Observation artifact from a CoinGecko market snapshot."""
    coin_id = str(snapshot.get("id", "unknown"))
    metrics: dict[str, float | int | str | None] = {
        "price_usd": _float_or_none(snapshot.get("current_price")),
        "change_24h_pct": _float_or_none(snapshot.get("price_change_percentage_24h")),
        "high_24h_usd": _float_or_none(snapshot.get("high_24h")),
        "low_24h_usd": _float_or_none(snapshot.get("low_24h")),
        "market_cap_usd": _float_or_none(snapshot.get("market_cap")),
        "volume_24h_usd": _float_or_none(snapshot.get("total_volume")),
    }
    if cross_validation is not None and cross_validation.quotes:
        metrics["consensus_price_usd"] = cross_validation.consensus_price_usd
        metrics["max_spread_pct"] = cross_validation.max_spread_pct
        metrics["venues_checked"] = len(cross_validation.quotes)
        for quote in cross_validation.quotes:
            metrics[f"{quote.venue}_price_usd"] = quote.price_usd

    return Observation(
        source="coingecko",
        endpoint="/coins/markets",
        entity=coin_id,
        observed_at=observed_at,
        fetched_at=fetched_at,
        freshness=freshness,
        age_s=max(0.0, age_s),
        metrics=metrics,
        urls=["https://www.coingecko.com"],
        cross_validation=cross_validation,
    )


class AsyncCoinGeckoClient:
    """Async CoinGecko client delegating caching & coalescing to CachingFeed."""

    BASE_URL = "https://api.coingecko.com/api/v3"

    def __init__(
        self,
        *,
        caching_feed: CachingFeed | None = None,
        http_client: httpx.AsyncClient | None = None,
        timeout: int = 15,
        cache_ttl: int = 60,
        api_key: str | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._timeout = timeout
        self._cache_ttl = cache_ttl
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._owns_client = http_client is None

        headers: dict[str, str] = {}
        if api_key:
            headers["x-cg-demo-api-key"] = api_key

        self._client = (
            http_client
            if http_client is not None
            else create_async_client(
                base_url=self.BASE_URL,
                timeout_s=float(timeout),
                headers=headers,
            )
        )

        if caching_feed is not None:
            self._feed: CachingFeed | None = caching_feed
        elif cache_ttl > 0:
            policies = dict(DEFAULT_POLICIES)
            if cache_ttl != 60:
                policies[CacheClass.MARKETS] = FreshnessPolicy(
                    fresh_ttl_s=cache_ttl,
                    stale_ttl_s=max(cache_ttl * 15, 900),
                )
            self._feed = CachingFeed(
                MemoryCacheStore(self._clock),
                MemoryLockManager(self._clock),
                self._clock,
                policies=policies,
            )
        else:
            self._feed = None

    @property
    def clock(self) -> Clock:
        return self._clock

    @classmethod
    def normalize_coin_id(cls, coin_id: str) -> str:
        return normalize_coin_id(coin_id)

    async def _fetch_raw_json(self, path: str, params: dict[str, Any]) -> Any:
        try:
            return await get_json_with_retry(self._client, path, params=params)
        except httpx.TimeoutException as exc:
            raise MarketDataError("CoinGecko timeout. Data belum tersedia.") from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 404:
                raise CoinNotFoundError(
                    "Coin ID tidak ditemukan di CoinGecko."
                ) from exc
            if status == 429:
                raise MarketDataError(
                    "CoinGecko sibuk atau terkena rate limit. Coba lagi beberapa saat."
                ) from exc
            raise MarketDataError(f"CoinGecko mengembalikan HTTP {status}.") from exc
        except httpx.RequestError as exc:
            raise MarketDataError(
                f"Gagal terhubung ke CoinGecko: {type(exc).__name__}."
            ) from exc
        except ValueError as exc:
            raise MarketDataError("Respons CoinGecko bukan JSON yang valid.") from exc

    async def _get_json(
        self,
        path: str,
        params: dict[str, Any],
        cache_key: str,
        *,
        cache_class: CacheClass = CacheClass.MARKETS,
    ) -> CachedPayload:
        """GET + JSON via CachingFeed with stale-while-revalidate & stale-if-error."""
        if self._feed is None:
            payload = await self._fetch_raw_json(path, params)
            return CachedPayload(
                value=payload,
                freshness=Freshness.FRESH,
                fetched_at=self._clock.now(),
                from_cache=False,
            )
        return await self._feed.get_or_fetch(
            key=cache_key,
            cache_class=cache_class,
            source="coingecko",
            fetcher=lambda: self._fetch_raw_json(path, params),
        )

    async def get_market_snapshot_with_provenance(self, coin_id: str) -> ProvenanceData:
        normalized = self.normalize_coin_id(coin_id)
        cached = await self._get_json(
            "/coins/markets",
            {"vs_currency": "usd", "ids": normalized, "price_change_percentage": "24h"},
            f"markets:{normalized}",
            cache_class=CacheClass.MARKETS,
        )
        payload = cached.value
        if (
            not isinstance(payload, list)
            or not payload
            or not isinstance(payload[0], dict)
        ):
            raise CoinNotFoundError(
                f"Coin ID '{normalized}' tidak ditemukan di CoinGecko."
            )
        snap = dict(payload[0])
        now = self._clock.now()
        age_s = max(0.0, (now - cached.fetched_at).total_seconds())
        observed_at = _parse_observed_at(snap.get("last_updated"), cached.fetched_at)
        return ProvenanceData(
            data=snap,
            fetched_at=cached.fetched_at,
            observed_at=observed_at,
            freshness=cached.freshness,
            age_s=age_s,
        )

    async def get_market_snapshot(self, coin_id: str) -> dict[str, Any]:
        prov = await self.get_market_snapshot_with_provenance(coin_id)
        snapshot: dict[str, Any] = prov.data
        return snapshot

    async def get_market_snapshots(
        self, coin_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        """Fetch multiple coins in a single batched ``/coins/markets`` request."""
        if not coin_ids:
            return {}
        normalized_ids = sorted({self.normalize_coin_id(cid) for cid in coin_ids})
        joined = ",".join(normalized_ids)
        cached = await self._get_json(
            "/coins/markets",
            {"vs_currency": "usd", "ids": joined, "price_change_percentage": "24h"},
            f"markets:batch:{joined}",
            cache_class=CacheClass.MARKETS,
        )
        payload = cached.value
        if not isinstance(payload, list):
            raise MarketDataError("Format respons batch markets CoinGecko tidak valid.")
        by_id: dict[str, dict[str, Any]] = {}
        for item in payload:
            if isinstance(item, dict):
                coin_id = item.get("id")
                if isinstance(coin_id, str):
                    by_id[coin_id] = dict(item)
        return by_id

    async def get_daily_closes_with_provenance(
        self, coin_id: str, days: int = 365
    ) -> ProvenanceData:
        normalized = self.normalize_coin_id(coin_id)
        cached = await self._get_json(
            f"/coins/{normalized}/market_chart",
            {"vs_currency": "usd", "days": days, "interval": "daily"},
            f"chart:{normalized}:{days}",
            cache_class=CacheClass.DAILY_CHART,
        )
        payload = cached.value
        rows = payload.get("prices") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows:
            raise MarketDataError("Data harga historis tidak tersedia.")
        try:
            closes = [
                float(row[1])  # type: ignore[arg-type]
                for row in rows
                if isinstance(row, list)
            ]

        except (IndexError, TypeError, ValueError) as exc:
            raise MarketDataError("Format data harga historis tidak dikenali.") from exc
        now = self._clock.now()
        age_s = max(0.0, (now - cached.fetched_at).total_seconds())
        return ProvenanceData(
            data=closes,
            fetched_at=cached.fetched_at,
            observed_at=cached.fetched_at,
            freshness=cached.freshness,
            age_s=age_s,
        )

    async def get_daily_closes(self, coin_id: str, days: int = 365) -> list[float]:
        """Daily closing prices (the last point is the current, partial day)."""
        prov = await self.get_daily_closes_with_provenance(coin_id, days=days)
        closes: list[float] = prov.data
        return closes

    async def get_ohlc_with_provenance(
        self, coin_id: str, days: int = 30
    ) -> ProvenanceData:
        normalized = self.normalize_coin_id(coin_id)
        cached = await self._get_json(
            f"/coins/{normalized}/ohlc",
            {"vs_currency": "usd", "days": days},
            f"ohlc:{normalized}:{days}",
            cache_class=CacheClass.OHLC_4H,
        )
        payload = cached.value
        if not isinstance(payload, list) or not payload:
            raise MarketDataError("Data OHLC tidak tersedia.")
        try:
            rows = [
                [float(x) for x in row[:5]]  # type: ignore[arg-type]
                for row in payload
                if isinstance(row, list)
            ]
        except (TypeError, ValueError) as exc:
            raise MarketDataError("Format data OHLC tidak dikenali.") from exc
        if len(rows) != len(payload) or any(len(row) != 5 for row in rows):
            raise MarketDataError("Format data OHLC tidak dikenali.")
        now = self._clock.now()
        age_s = max(0.0, (now - cached.fetched_at).total_seconds())
        return ProvenanceData(
            data=rows,
            fetched_at=cached.fetched_at,
            observed_at=cached.fetched_at,
            freshness=cached.freshness,
            age_s=age_s,
        )

    async def get_ohlc(self, coin_id: str, days: int = 30) -> list[list[float]]:
        """OHLC candles as ``[timestamp_ms, open, high, low, close]`` rows."""
        prov = await self.get_ohlc_with_provenance(coin_id, days=days)
        rows: list[list[float]] = prov.data
        return rows

    async def aclose(self) -> None:
        """Close pending background cache refreshes and owned HTTP client."""
        if self._feed is not None:
            await self._feed.aclose()
        if self._owns_client:
            await self._client.aclose()


class CoinGeckoClient:
    """Legacy synchronous CoinGecko client preserved during migration."""

    BASE_URL = "https://api.coingecko.com/api/v3"
    _COIN_ID_RE = _COIN_ID_RE

    def __init__(
        self,
        timeout: int = 15,
        cache_ttl: int = 60,
        api_key: str | None = None,
    ) -> None:
        self._timeout = timeout
        self._cache_ttl = cache_ttl
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

        retry = Retry(
            total=3,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
            respect_retry_after_header=True,
        )
        self._session = requests.Session()
        self._session.mount("https://", HTTPAdapter(max_retries=retry))
        self._session.headers.update(
            {
                "accept": "application/json",
                "user-agent": f"alphamacro-crypto/{__version__}",
            }
        )
        if api_key:
            self._session.headers["x-cg-demo-api-key"] = api_key

    @classmethod
    def normalize_coin_id(cls, coin_id: str) -> str:
        return normalize_coin_id(coin_id)

    def _cache_get(self, key: str) -> Any | None:
        if self._cache_ttl <= 0:
            return None
        with self._lock:
            item = self._cache.get(key)
            if item and time.monotonic() - item[0] < self._cache_ttl:
                return item[1]
            self._cache.pop(key, None)
        return None

    def _cache_set(self, key: str, value: Any) -> None:
        if self._cache_ttl > 0:
            with self._lock:
                self._cache[key] = (time.monotonic(), value)

    def _get_json(self, path: str, params: dict[str, Any], cache_key: str) -> Any:
        """GET + JSON with TTL cache and uniform error mapping."""
        cached = self._cache_get(cache_key)
        if cached is not None:
            logger.debug("CoinGecko cache hit: %s", cache_key)
            return cached
        try:
            response = self._session.get(
                f"{self.BASE_URL}{path}", params=params, timeout=self._timeout
            )
            response.raise_for_status()
            payload = response.json()
        except requests.exceptions.Timeout as exc:
            raise MarketDataError("CoinGecko timeout. Data belum tersedia.") from exc
        except requests.exceptions.RetryError as exc:
            raise MarketDataError(
                "CoinGecko sibuk atau terkena rate limit. Coba lagi beberapa saat."
            ) from exc
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            if status == 404:
                raise CoinNotFoundError(
                    "Coin ID tidak ditemukan di CoinGecko."
                ) from exc
            raise MarketDataError(f"CoinGecko mengembalikan HTTP {status}.") from exc
        except requests.exceptions.RequestException as exc:
            raise MarketDataError(
                f"Gagal terhubung ke CoinGecko: {type(exc).__name__}."
            ) from exc
        except ValueError as exc:
            raise MarketDataError("Respons CoinGecko bukan JSON yang valid.") from exc
        if payload:
            self._cache_set(cache_key, payload)
        return payload

    def get_market_snapshot(self, coin_id: str) -> dict[str, Any]:
        coin_id = self.normalize_coin_id(coin_id)
        payload = self._get_json(
            "/coins/markets",
            {"vs_currency": "usd", "ids": coin_id, "price_change_percentage": "24h"},
            f"markets:{coin_id}",
        )
        if not isinstance(payload, list) or not payload:
            raise CoinNotFoundError(
                f"Coin ID '{coin_id}' tidak ditemukan di CoinGecko."
            )
        snapshot: dict[str, Any] = payload[0]
        return snapshot

    def get_daily_closes(self, coin_id: str, days: int = 365) -> list[float]:
        """Daily closing prices (the last point is the current, partial day)."""
        coin_id = self.normalize_coin_id(coin_id)
        payload = self._get_json(
            f"/coins/{coin_id}/market_chart",
            {"vs_currency": "usd", "days": days, "interval": "daily"},
            f"chart:{coin_id}:{days}",
        )
        rows = payload.get("prices") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows:
            raise MarketDataError("Data harga historis tidak tersedia.")
        try:
            return [float(row[1]) for row in rows]
        except (IndexError, TypeError, ValueError) as exc:
            raise MarketDataError("Format data harga historis tidak dikenali.") from exc

    def get_ohlc(self, coin_id: str, days: int = 30) -> list[list[float]]:
        """OHLC candles as ``[timestamp_ms, open, high, low, close]`` rows."""
        coin_id = self.normalize_coin_id(coin_id)
        payload = self._get_json(
            f"/coins/{coin_id}/ohlc",
            {"vs_currency": "usd", "days": days},
            f"ohlc:{coin_id}:{days}",
        )
        if not isinstance(payload, list) or not payload:
            raise MarketDataError("Data OHLC tidak tersedia.")
        try:
            rows = [[float(x) for x in row[:5]] for row in payload]
        except (TypeError, ValueError) as exc:
            raise MarketDataError("Format data OHLC tidak dikenali.") from exc
        if any(len(row) != 5 for row in rows):
            raise MarketDataError("Format data OHLC tidak dikenali.")
        return rows


def _fmt_number(value: Any, decimals: int = 2) -> str:
    try:
        return f"{float(value):,.{decimals}f}"
    except (TypeError, ValueError):
        return "N/A"


def _fmt_usd(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "N/A"
    decimals = 2 if abs(number) >= 1 else 6
    return f"${number:,.{decimals}f}"


def format_market_snapshot(
    snapshot: dict[str, Any],
    retrieved_at: str,
    cross_validation: CrossValidationReport | None = None,
) -> str:
    """Render a snapshot as plain text for the LLM (keeps timestamps and source)."""
    name = snapshot.get("name", snapshot.get("id", "N/A"))
    lines = [
        f"CRYPTO MARKET DATA: {name} ({str(snapshot.get('symbol', '')).upper()})",
        f"Coin ID: {snapshot.get('id', 'N/A')}",
        f"Price USD: {_fmt_usd(snapshot.get('current_price'))}",
        f"24H Change: {_fmt_number(snapshot.get('price_change_percentage_24h'))}%",
        f"24H High: {_fmt_usd(snapshot.get('high_24h'))}",
        f"24H Low: {_fmt_usd(snapshot.get('low_24h'))}",
        f"Market Cap: {_fmt_usd(snapshot.get('market_cap'))}",
        f"24H Volume: {_fmt_usd(snapshot.get('total_volume'))}",
        f"Source Last Updated: {snapshot.get('last_updated', 'Tidak tersedia')}",
        f"Retrieved At: {retrieved_at}",
        "Source: CoinGecko API (https://www.coingecko.com)",
    ]
    if cross_validation is not None and cross_validation.quotes:
        venue_str = ", ".join(
            f"{q.venue}={_fmt_usd(q.price_usd)}" for q in cross_validation.quotes
        )
        status = "CONSISTENT" if cross_validation.is_consistent else "DIVERGENT"
        lines.append(
            f"Multi-Venue Cross-Validation: {status} | "
            f"Consensus: {_fmt_usd(cross_validation.consensus_price_usd)} | "
            f"Max Spread: {cross_validation.max_spread_pct:.2f}% | "
            f"Venues: {venue_str}"
        )
    return "\n".join(lines)


def build_async_market_data_tool(
    client: AsyncCoinGeckoClient,
    *,
    validator: MultiVenueValidator | None = None,
):  # noqa: ANN201
    """Wrap an :class:`AsyncCoinGeckoClient` as an async tool emitting Observation."""

    @tool(MARKET_TOOL_NAME, response_format="content_and_artifact")
    async def get_crypto_market_data(
        coin_id: str = "bitcoin",
    ) -> tuple[str, Observation | None]:
        """Fetch cryptocurrency price (USD), 24h change, market cap, volume and
        24h high/low from CoinGecko (with multi-venue spot cross-validation).

        Args:
            coin_id: CoinGecko coin ID, e.g. 'bitcoin', 'ethereum', 'solana'.
        """
        try:
            prov = await client.get_market_snapshot_with_provenance(coin_id)
            snapshot = prov.data
            retrieved = prov.fetched_at.astimezone(WIB).strftime(
                "%Y-%m-%d %H:%M:%S WIB"
            )
            cv: CrossValidationReport | None = None
            ref_price = _float_or_none(snapshot.get("current_price"))
            if validator is not None and ref_price is not None and ref_price > 0:
                normalized = normalize_coin_id(coin_id)
                cv = await validator.cross_validate(
                    normalized,
                    reference_price_usd=ref_price,
                    reference_venue="coingecko",
                )
            obs = build_market_observation(
                snapshot,
                fetched_at=prov.fetched_at,
                observed_at=prov.observed_at,
                freshness=prov.freshness,
                age_s=prov.age_s,
                cross_validation=cv,
            )
            return format_market_snapshot(snapshot, retrieved, cross_validation=cv), obs
        except MarketDataError as exc:
            logger.warning("Market data tool failed: %s", exc)
            return f"TOOL ERROR: {exc}", None
        except Exception:  # tools must never crash the agent loop
            logger.exception("Unexpected error in market data tool")
            return "TOOL ERROR: kesalahan internal saat mengambil data pasar.", None

    return get_crypto_market_data


def build_market_data_tool(client: CoinGeckoClient):  # noqa: ANN201
    """Wrap a legacy sync :class:`CoinGeckoClient` as a tool emitting Observation."""

    @tool(MARKET_TOOL_NAME, response_format="content_and_artifact")
    def get_crypto_market_data(
        coin_id: str = "bitcoin",
    ) -> tuple[str, Observation | None]:
        """Fetch cryptocurrency price (USD), 24h change, market cap, volume and
        24h high/low from CoinGecko.

        Args:
            coin_id: CoinGecko coin ID, e.g. 'bitcoin', 'ethereum', 'solana'.
        """
        try:
            snapshot = client.get_market_snapshot(coin_id)
            now = datetime.now(UTC)
            retrieved = now.astimezone(WIB).strftime("%Y-%m-%d %H:%M:%S WIB")
            observed_at = _parse_observed_at(snapshot.get("last_updated"), now)
            obs = build_market_observation(
                snapshot,
                fetched_at=now,
                observed_at=observed_at,
                freshness=Freshness.FRESH,
                age_s=0.0,
            )
            return format_market_snapshot(snapshot, retrieved), obs
        except MarketDataError as exc:
            logger.warning("Market data tool failed: %s", exc)
            return f"TOOL ERROR: {exc}", None
        except Exception:  # tools must never crash the agent loop
            logger.exception("Unexpected error in market data tool")
            return "TOOL ERROR: kesalahan internal saat mengambil data pasar.", None

    return get_crypto_market_data
