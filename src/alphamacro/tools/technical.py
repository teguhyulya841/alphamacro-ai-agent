"""Technical-indicator tool: indicators are computed by code, never by the LLM."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from langchain_core.tools import tool

from alphamacro.constants import TECHNICAL_TOOL_NAME, WIB
from alphamacro.exceptions import MarketDataError
from alphamacro.models import Freshness, Observation
from alphamacro.quant.snapshot import build_snapshot, format_snapshot
from alphamacro.tools.coingecko import (
    AsyncCoinGeckoClient,
    CoinGeckoClient,
    ProvenanceData,
    normalize_coin_id,
)

logger = logging.getLogger(__name__)

VALID_TIMEFRAMES = ("daily", "4h")


def _build_technical_observation(
    snapshot: dict[str, Any],
    *,
    endpoint: str,
    fetched_at: datetime,
    observed_at: datetime,
    freshness: Freshness,
    age_s: float,
) -> Observation:
    numeric_keys = (
        "price",
        "sma20",
        "sma50",
        "sma200",
        "ema12",
        "ema26",
        "rsi14",
        "macd",
        "macd_signal",
        "macd_histogram",
        "bollinger_upper",
        "bollinger_middle",
        "bollinger_lower",
        "percent_b",
        "realized_volatility_annualized_pct",
        "max_drawdown_pct",
        "atr14",
        "atr14_pct_of_price",
        "nearest_support",
        "nearest_resistance",
    )
    metrics: dict[str, float | int | str | None] = {
        "timeframe": str(snapshot.get("timeframe", "daily")),
        "candles": int(snapshot.get("candles", 0)),
    }
    for key in numeric_keys:
        val = snapshot.get(key)
        metrics[key] = None if val is None else float(val)

    return Observation(
        source="coingecko",
        endpoint=endpoint,
        entity=str(snapshot.get("coin_id", "unknown")),
        observed_at=observed_at,
        fetched_at=fetched_at,
        freshness=freshness,
        age_s=max(0.0, age_s),
        metrics=metrics,
        urls=["https://www.coingecko.com"],
    )


async def async_fetch_snapshot_with_provenance(
    client: AsyncCoinGeckoClient, coin_id: str, timeframe: str = "daily"
) -> tuple[dict[str, Any], Observation]:
    """Async fetch + offloaded computation returning both snapshot and Observation."""
    timeframe = (timeframe or "").lower().strip()
    if timeframe not in VALID_TIMEFRAMES:
        raise MarketDataError("timeframe harus 'daily' atau '4h'.")
    normalized = normalize_coin_id(coin_id)
    try:
        if timeframe == "daily":
            prov: ProvenanceData = await client.get_daily_closes_with_provenance(
                normalized, days=365
            )
            snapshot = await asyncio.to_thread(
                build_snapshot, normalized, timeframe, prov.data
            )
            endpoint = f"/coins/{normalized}/market_chart"
        else:
            prov = await client.get_ohlc_with_provenance(normalized, days=30)
            rows = prov.data
            snapshot = await asyncio.to_thread(
                build_snapshot,
                normalized,
                timeframe,
                [r[4] for r in rows],
                [r[2] for r in rows],
                [r[3] for r in rows],
            )
            endpoint = f"/coins/{normalized}/ohlc"
    except ValueError as exc:
        raise MarketDataError(str(exc)) from exc

    obs = _build_technical_observation(
        snapshot,
        endpoint=endpoint,
        fetched_at=prov.fetched_at,
        observed_at=prov.observed_at,
        freshness=prov.freshness,
        age_s=prov.age_s,
    )
    return snapshot, obs


async def async_fetch_snapshot(
    client: AsyncCoinGeckoClient, coin_id: str, timeframe: str = "daily"
) -> dict[str, Any]:
    """Async fetch + offloaded indicator computation for the async agent & API."""
    snapshot, _ = await async_fetch_snapshot_with_provenance(
        client, coin_id, timeframe
    )
    return snapshot


def fetch_snapshot(
    client: CoinGeckoClient, coin_id: str, timeframe: str = "daily"
) -> dict[str, Any]:
    """Legacy synchronous snapshot helper (shared by sync tools and tests)."""
    timeframe = (timeframe or "").lower().strip()
    if timeframe not in VALID_TIMEFRAMES:
        raise MarketDataError("timeframe harus 'daily' atau '4h'.")
    normalized = normalize_coin_id(coin_id)
    try:
        if timeframe == "daily":
            closes = client.get_daily_closes(normalized, days=365)
            return build_snapshot(normalized, timeframe, closes)
        rows = client.get_ohlc(normalized, days=30)
        return build_snapshot(
            normalized,
            timeframe,
            closes=[r[4] for r in rows],
            highs=[r[2] for r in rows],
            lows=[r[3] for r in rows],
        )
    except ValueError as exc:
        raise MarketDataError(str(exc)) from exc


def build_async_technical_tool(client: AsyncCoinGeckoClient):  # noqa: ANN201
    """Wrap an :class:`AsyncCoinGeckoClient` as an async tool emitting Observation."""

    @tool(TECHNICAL_TOOL_NAME, response_format="content_and_artifact")
    async def get_technical_indicators(
        coin_id: str = "bitcoin", timeframe: str = "daily"
    ) -> tuple[str, Observation | None]:
        """Compute technical indicators from real market data: SMA 20/50/200, EMA,
        RSI 14, MACD, Bollinger Bands, ATR, swing support/resistance, volatility and
        drawdown. ALWAYS use this tool for any technical-analysis number; never
        calculate them yourself.

        Args:
            coin_id: CoinGecko coin ID, e.g. 'bitcoin', 'ethereum'.
            timeframe: 'daily' (365 daily closes) or '4h' (30 days of 4h OHLC).
        """
        try:
            snapshot, obs = await async_fetch_snapshot_with_provenance(
                client, coin_id, timeframe
            )
            retrieved = obs.fetched_at.astimezone(WIB).strftime(
                "%Y-%m-%d %H:%M:%S WIB"
            )
            return format_snapshot(snapshot, retrieved), obs
        except MarketDataError as exc:
            logger.warning("Technical tool failed: %s", exc)
            return f"TOOL ERROR: {exc}", None
        except Exception:
            logger.exception("Unexpected error in technical tool")
            return "TOOL ERROR: kesalahan internal saat menghitung indikator.", None

    return get_technical_indicators


def build_technical_tool(client: CoinGeckoClient):  # noqa: ANN201
    """Legacy synchronous technical-indicator tool emitting Observation."""

    @tool(TECHNICAL_TOOL_NAME, response_format="content_and_artifact")
    def get_technical_indicators(
        coin_id: str = "bitcoin", timeframe: str = "daily"
    ) -> tuple[str, Observation | None]:
        """Compute technical indicators from real market data: SMA 20/50/200, EMA,
        RSI 14, MACD, Bollinger Bands, ATR, swing support/resistance, volatility and
        drawdown. ALWAYS use this tool for any technical-analysis number; never
        calculate them yourself.

        Args:
            coin_id: CoinGecko coin ID, e.g. 'bitcoin', 'ethereum'.
            timeframe: 'daily' (365 daily closes) or '4h' (30 days of 4h OHLC).
        """
        try:
            snapshot = fetch_snapshot(client, coin_id, timeframe)
            now = datetime.now(UTC)
            retrieved = now.astimezone(WIB).strftime("%Y-%m-%d %H:%M:%S WIB")
            normalized = normalize_coin_id(coin_id)
            endpoint = (
                f"/coins/{normalized}/market_chart"
                if (timeframe or "daily").lower().strip() == "daily"
                else f"/coins/{normalized}/ohlc"
            )
            obs = _build_technical_observation(
                snapshot,
                endpoint=endpoint,
                fetched_at=now,
                observed_at=now,
                freshness=Freshness.FRESH,
                age_s=0.0,
            )
            return format_snapshot(snapshot, retrieved), obs
        except MarketDataError as exc:
            logger.warning("Technical tool failed: %s", exc)
            return f"TOOL ERROR: {exc}", None
        except Exception:
            logger.exception("Unexpected error in technical tool")
            return "TOOL ERROR: kesalahan internal saat menghitung indikator.", None

    return get_technical_indicators
