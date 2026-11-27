"""Build a technical-analysis snapshot from price data (deterministic, auditable)."""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import AwareDatetime

from alphamacro.models import Freshness, IndicatorSnapshot
from alphamacro.quant import indicators as ind

# Periods per year used to annualise volatility for each timeframe.
PERIODS_PER_YEAR = {"daily": 365, "4h": 365 * 6}

# Conventional thresholds. They describe state; they do not predict direction
RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD = 30.0


def _last(series: Sequence[float | None]) -> float | None:
    return series[-1] if series else None


def _label_rsi(value: float | None) -> str:
    if value is None:
        return "N/A"
    if value >= RSI_OVERBOUGHT:
        return "overbought (>= 70, konvensi umum)"
    if value <= RSI_OVERSOLD:
        return "oversold (<= 30, konvensi umum)"
    return "netral (30-70)"


def _recent_macd_cross(hist: Sequence[float | None], lookback: int = 3) -> str:
    tail = [h for h in hist[-(lookback + 1) :] if h is not None]
    if len(tail) < 2:
        return "N/A"

    for prev, cur in itertools.pairwise(tail):
        if prev <= 0 < cur:
            return f"bullish crossover dalam {lookback} candle terakhir"
        if prev >= 0 > cur:
            return f"bearish crossover dalam {lookback} candle terakhir"
    return "tidak ada crossover baru"


def _trend_label(price: float, sma50: float | None, sma200: float | None) -> str:
    if sma50 is None and sma200 is None:
        return "N/A (data tidak cukup untuk SMA 50/200)"
    parts = []
    if sma50 is not None:
        parts.append("di atas SMA50" if price > sma50 else "di bawah SMA50")
    if sma200 is not None:
        parts.append("di atas SMA200" if price > sma200 else "di bawah SMA200")
    return " dan ".join(parts)


def build_snapshot(
    coin_id: str,
    timeframe: str,
    closes: Sequence[float],
    highs: Sequence[float] | None = None,
    lows: Sequence[float] | None = None,
) -> dict[str, Any]:
    """Compute indicators.
    ``highs``/``lows`` enable ATR and swing support/resistance.
    """
    if len(closes) < 30:
        raise ValueError(
            "Data harga tidak cukup (minimal 30 titik) untuk menghitung indikator."
        )
    if any(not math.isfinite(c) or c <= 0 for c in closes):
        raise ValueError(
            "Data harga mengandung nilai tidak valid (non-positif atau NaN/Inf)."
        )
    if timeframe not in PERIODS_PER_YEAR:
        raise ValueError(f"timeframe harus salah satu dari {sorted(PERIODS_PER_YEAR)}")

    price = float(closes[-1])
    macd_line, signal_line, hist = ind.macd(closes)
    mid, upper, lower = ind.bollinger(closes)
    sma50, sma200 = _last(ind.sma(closes, 50)), _last(ind.sma(closes, 200))
    bb_upper, bb_lower = _last(upper), _last(lower)
    percent_b = None
    if bb_upper is not None and bb_lower is not None and bb_upper != bb_lower:
        percent_b = (price - bb_lower) / (bb_upper - bb_lower)

    snapshot: dict[str, Any] = {
        "coin_id": coin_id,
        "timeframe": timeframe,
        "candles": len(closes),
        "price": price,
        "sma20": _last(ind.sma(closes, 20)),
        "sma50": sma50,
        "sma200": sma200,
        "ema12": _last(ind.ema(closes, 12)),
        "ema26": _last(ind.ema(closes, 26)),
        "rsi14": _last(ind.rsi(closes, 14)),
        "macd": _last(macd_line),
        "macd_signal": _last(signal_line),
        "macd_histogram": _last(hist),
        "macd_state": _recent_macd_cross(hist),
        "bollinger_upper": bb_upper,
        "bollinger_middle": _last(mid),
        "bollinger_lower": bb_lower,
        "percent_b": percent_b,
        "realized_volatility_annualized_pct": None,
        "max_drawdown_pct": None,
        "atr14": None,
        "atr14_pct_of_price": None,
        "nearest_support": None,
        "nearest_resistance": None,
        "trend": _trend_label(price, sma50, sma200),
    }
    snapshot["rsi14_state"] = _label_rsi(snapshot["rsi14"])

    vol = ind.realized_volatility(closes, PERIODS_PER_YEAR[timeframe])
    if vol is not None:
        snapshot["realized_volatility_annualized_pct"] = vol * 100
    drawdown = ind.max_drawdown(closes)
    if drawdown is not None:
        snapshot["max_drawdown_pct"] = drawdown * 100

    if (
        highs is not None
        and lows is not None
        and len(highs) == len(lows) == len(closes)
    ):
        atr_value = _last(ind.atr(highs, lows, closes, 14))
        if atr_value is not None:
            snapshot["atr14"] = atr_value
            if price > 0:
                snapshot["atr14_pct_of_price"] = atr_value / price * 100
        swing_highs, swing_lows = ind.swing_points(highs, lows, window=3)
        support, resistance = ind.nearest_levels(price, swing_highs, swing_lows)
        snapshot["nearest_support"] = support
        snapshot["nearest_resistance"] = resistance
    return snapshot


def build_typed_snapshot(
    coin_id: str,
    timeframe: Literal["daily", "4h"],
    closes: Sequence[float],
    highs: Sequence[float] | None = None,
    lows: Sequence[float] | None = None,
    *,
    as_of: AwareDatetime | None = None,
    source: str = "coingecko",
    freshness: Freshness = Freshness.FRESH,
    age_s: float = 0.0,
) -> IndicatorSnapshot:
    """Compute indicators and wrap them in a validated IndicatorSnapshot model."""
    raw = build_snapshot(coin_id, timeframe, closes, highs=highs, lows=lows)
    effective_as_of = as_of if as_of is not None else datetime.now(UTC)
    return IndicatorSnapshot(
        as_of=effective_as_of,
        source=source,
        freshness=freshness,
        age_s=age_s,
        **raw,
    )


def _fmt(value: float | None, decimals: int = 2) -> str:
    return "N/A" if value is None else f"{value:,.{decimals}f}"


def _fmt_price(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"${value:,.2f}" if abs(value) >= 1 else f"${value:,.6f}"


def format_snapshot(snapshot: dict[str, Any], retrieved_at: str) -> str:
    """Plain-text rendering for the LLM.
    Every number here is a computed, citable fact."""
    s = snapshot
    return (
        f"TECHNICAL INDICATORS (computed from market data): {s['coin_id']} | "
        f"{s['timeframe']}\n"
        f"Candles Used: {s['candles']}\n"
        f"Last Close USD: {_fmt_price(s['price'])}\n"
        f"SMA 20: {_fmt_price(s['sma20'])}\n"
        f"SMA 50: {_fmt_price(s['sma50'])}\n"
        f"SMA 200: {_fmt_price(s['sma200'])}\n"
        f"EMA 12: {_fmt_price(s['ema12'])}\n"
        f"EMA 26: {_fmt_price(s['ema26'])}\n"
        f"Trend vs SMA: {s['trend']}\n"
        f"RSI 14: {_fmt(s['rsi14'])} ({s['rsi14_state']})\n"
        f"MACD (12,26,9): {_fmt(s['macd'], 4)} | Signal: {_fmt(s['macd_signal'], 4)} | "
        f"Histogram: {_fmt(s['macd_histogram'], 4)}\n"
        f"MACD State: {s['macd_state']}\n"
        f"Bollinger (20,2) Upper: {_fmt_price(s['bollinger_upper'])} | "
        f"Middle: {_fmt_price(s['bollinger_middle'])} | "
        f"Lower: {_fmt_price(s['bollinger_lower'])}\n"
        f"Bollinger %B: {_fmt(s['percent_b'], 3)}\n"
        f"ATR 14: {_fmt_price(s['atr14'])} "
        f"({_fmt(s['atr14_pct_of_price'])}% of price)\n"
        f"Nearest Swing Support: {_fmt_price(s['nearest_support'])}\n"
        f"Nearest Swing Resistance: {_fmt_price(s['nearest_resistance'])}\n"
        f"Annualized Realized Volatility: "
        f"{_fmt(s['realized_volatility_annualized_pct'])}%\n"
        f"Max Drawdown In Window: {_fmt(s['max_drawdown_pct'])}%\n"
        f"Retrieved At: {retrieved_at}\n"
        "Method: deterministic code (Wilder RSI/ATR, EMA seeded by SMA). Labels follow "
        "common conventions and are not predictions.\n"
        "Source: CoinGecko API (https://www.coingecko.com)"
    )
