"""Multi-venue spot price clients (Binance & Coinbase) and cross-validation."""

from __future__ import annotations

import asyncio
import logging
import statistics
from collections.abc import Sequence

import httpx

from alphamacro.clock import SystemClock
from alphamacro.infra.caching import CachingFeed
from alphamacro.infra.http import create_async_client, get_json_with_retry
from alphamacro.models import (
    CacheClass,
    CrossValidationReport,
    Freshness,
    VenueQuote,
)
from alphamacro.ports import Clock

logger = logging.getLogger(__name__)

# Canonical mapping from CoinGecko coin_id to exchange spot symbols.
BINANCE_SYMBOLS: dict[str, str] = {
    "bitcoin": "BTCUSDT",
    "ethereum": "ETHUSDT",
    "solana": "SOLUSDT",
    "ripple": "XRPUSDT",
    "binancecoin": "BNBUSDT",
    "cardano": "ADAUSDT",
    "dogecoin": "DOGEUSDT",
    "avalanche-2": "AVAXUSDT",
    "chainlink": "LINKUSDT",
    "polkadot": "DOTUSDT",
    "tron": "TRXUSDT",
    "toncoin": "TONUSDT",
    "sui": "SUIUSDT",
    "aptos": "APTUSDT",
    "near": "NEARUSDT",
}

COINBASE_PAIRS: dict[str, str] = {
    "bitcoin": "BTC-USD",
    "ethereum": "ETH-USD",
    "solana": "SOL-USD",
    "ripple": "XRP-USD",
    "cardano": "ADA-USD",
    "dogecoin": "DOGE-USD",
    "avalanche-2": "AVAX-USD",
    "chainlink": "LINK-USD",
    "polkadot": "DOT-USD",
    "sui": "SUI-USD",
    "aptos": "APT-USD",
    "near": "NEAR-USD",
}


class BinanceSpotClient:
    """Read-only async client for Binance public spot ticker endpoint."""

    BASE_URL = "https://api.binance.com"

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        caching_feed: CachingFeed | None = None,
        timeout_s: float = 8.0,
        clock: Clock | None = None,
    ) -> None:
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._feed = caching_feed
        self._owns_client = http_client is None
        self._client = (
            http_client
            if http_client is not None
            else create_async_client(base_url=self.BASE_URL, timeout_s=timeout_s)
        )

    async def get_quote(self, coin_id: str) -> VenueQuote | None:
        symbol = BINANCE_SYMBOLS.get(coin_id.lower().strip())
        if not symbol:
            return None
        try:
            if self._feed is not None:
                cached = await self._feed.get_or_fetch(
                    key=f"venue:binance:{symbol}",
                    cache_class=CacheClass.TICKER,
                    source="binance",
                    fetcher=lambda: get_json_with_retry(
                        self._client,
                        "/api/v3/ticker/price",
                        params={"symbol": symbol},
                        max_retries=1,
                        base_backoff_s=0.25,
                    ),
                )
                payload = cached.value
                fetched_at = cached.fetched_at
                freshness = cached.freshness
            else:
                payload = await get_json_with_retry(
                    self._client,
                    "/api/v3/ticker/price",
                    params={"symbol": symbol},
                    max_retries=1,
                    base_backoff_s=0.25,
                )
                fetched_at = self._clock.now()
                freshness = Freshness.FRESH

            if not isinstance(payload, dict):
                return None
            raw_price = payload.get("price")
            if not isinstance(raw_price, (int, float, str)) or isinstance(
                raw_price, bool
            ):
                return None
            price = float(raw_price)
            if price <= 0:
                return None
            now = self._clock.now()
            age_s = max(0.0, (now - fetched_at).total_seconds())
            return VenueQuote(
                venue="binance",
                symbol=symbol,
                price_usd=price,
                observed_at=fetched_at,
                fetched_at=fetched_at,
                freshness=freshness,
                age_s=age_s,
            )
        except Exception as exc:
            logger.warning(
                "Binance spot quote unavailable for %s (%s): %s",
                coin_id,
                symbol,
                type(exc).__name__,
            )
            return None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class CoinbaseSpotClient:
    """Read-only async client for Coinbase public spot price endpoint."""

    BASE_URL = "https://api.coinbase.com"

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        caching_feed: CachingFeed | None = None,
        timeout_s: float = 8.0,
        clock: Clock | None = None,
    ) -> None:
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._feed = caching_feed
        self._owns_client = http_client is None
        self._client = (
            http_client
            if http_client is not None
            else create_async_client(base_url=self.BASE_URL, timeout_s=timeout_s)
        )

    async def get_quote(self, coin_id: str) -> VenueQuote | None:
        pair = COINBASE_PAIRS.get(coin_id.lower().strip())
        if not pair:
            return None
        path = f"/v2/prices/{pair}/spot"
        try:
            if self._feed is not None:
                cached = await self._feed.get_or_fetch(
                    key=f"venue:coinbase:{pair}",
                    cache_class=CacheClass.TICKER,
                    source="coinbase",
                    fetcher=lambda: get_json_with_retry(
                        self._client,
                        path,
                        max_retries=1,
                        base_backoff_s=0.25,
                    ),
                )
                payload = cached.value
                fetched_at = cached.fetched_at
                freshness = cached.freshness
            else:
                payload = await get_json_with_retry(
                    self._client,
                    path,
                    max_retries=1,
                    base_backoff_s=0.25,
                )
                fetched_at = self._clock.now()
                freshness = Freshness.FRESH

            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict):
                return None
            raw_amount = data.get("amount")
            if not isinstance(raw_amount, (int, float, str)) or isinstance(
                raw_amount, bool
            ):
                return None
            price = float(raw_amount)
            if price <= 0:
                return None
            now = self._clock.now()
            age_s = max(0.0, (now - fetched_at).total_seconds())
            return VenueQuote(
                venue="coinbase",
                symbol=pair,
                price_usd=price,
                observed_at=fetched_at,
                fetched_at=fetched_at,
                freshness=freshness,
                age_s=age_s,
            )
        except Exception as exc:
            logger.warning(
                "Coinbase spot quote unavailable for %s (%s): %s",
                coin_id,
                pair,
                type(exc).__name__,
            )
            return None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def evaluate_cross_validation(
    *,
    coin_id: str,
    reference_price_usd: float,
    quotes: Sequence[VenueQuote],
    reference_venue: str = "coingecko",
    max_spread_threshold_pct: float = 1.5,
) -> CrossValidationReport:
    """Pure deterministic spread & consensus evaluator across venues."""
    valid_quotes = [q for q in quotes if q.price_usd > 0]
    all_prices = [reference_price_usd, *(q.price_usd for q in valid_quotes)]
    consensus = float(statistics.median(all_prices))

    max_spread = 0.0
    divergent: list[str] = []
    warnings: list[str] = []

    for quote in valid_quotes:
        spread_pct = (
            abs(quote.price_usd - reference_price_usd) / reference_price_usd * 100.0
        )
        if spread_pct > max_spread:
            max_spread = spread_pct
        if spread_pct > max_spread_threshold_pct:
            divergent.append(quote.venue)
            warnings.append(
                f"Divergensi harga {coin_id}: {reference_venue} "
                f"(${reference_price_usd:,.2f}) vs {quote.venue} "
                f"(${quote.price_usd:,.2f}) berselisih {spread_pct:.2f}% "
                f"(batas {max_spread_threshold_pct:.2f}%)."
            )

    return CrossValidationReport(
        coin_id=coin_id,
        reference_venue=reference_venue,
        reference_price_usd=reference_price_usd,
        quotes=list(valid_quotes),
        consensus_price_usd=round(consensus, 6),
        max_spread_pct=round(max_spread, 4),
        divergent_venues=divergent,
        is_consistent=len(divergent) == 0,
        warnings=warnings,
    )


class MultiVenueValidator:
    """Concurrently queries secondary venues and cross-validates spot prices."""

    def __init__(
        self,
        *,
        binance: BinanceSpotClient | None = None,
        coinbase: CoinbaseSpotClient | None = None,
        caching_feed: CachingFeed | None = None,
        clock: Clock | None = None,
        max_spread_threshold_pct: float = 1.5,
    ) -> None:
        effective_clock = clock if clock is not None else SystemClock()
        self._binance = (
            binance
            if binance is not None
            else BinanceSpotClient(caching_feed=caching_feed, clock=effective_clock)
        )
        self._coinbase = (
            coinbase
            if coinbase is not None
            else CoinbaseSpotClient(caching_feed=caching_feed, clock=effective_clock)
        )
        self._threshold_pct = max_spread_threshold_pct

    async def fetch_quotes(self, coin_id: str) -> list[VenueQuote]:
        results = await asyncio.gather(
            self._binance.get_quote(coin_id),
            self._coinbase.get_quote(coin_id),
            return_exceptions=True,
        )
        quotes: list[VenueQuote] = [
            item for item in results if isinstance(item, VenueQuote)
        ]
        return quotes

    async def cross_validate(
        self,
        coin_id: str,
        *,
        reference_price_usd: float,
        reference_venue: str = "coingecko",
        max_spread_threshold_pct: float | None = None,
    ) -> CrossValidationReport:
        quotes = await self.fetch_quotes(coin_id)
        threshold = (
            max_spread_threshold_pct
            if max_spread_threshold_pct is not None
            else self._threshold_pct
        )
        return evaluate_cross_validation(
            coin_id=coin_id,
            reference_price_usd=reference_price_usd,
            quotes=quotes,
            reference_venue=reference_venue,
            max_spread_threshold_pct=threshold,
        )

    async def aclose(self) -> None:
        await asyncio.gather(
            self._binance.aclose(),
            self._coinbase.aclose(),
            return_exceptions=True,
        )
