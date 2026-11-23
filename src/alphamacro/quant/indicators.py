"""Pure-Python technical indicators (no numpy/pandas dependency).

Conventions
-----------
* Every series function returns a list the same length as its input, with ``None`` during the
  warm-up period, so indices stay aligned with the input data.
* RSI/ATR use Wilder's smoothing; EMA is seeded with the SMA of the first ``period`` values
  (the most common charting-package convention).
"""

from __future__ import annotations

import math
from collections.abc import Sequence

Series = list["float | None"]


def _check_period(period: int) -> None:
    if period < 1:
        raise ValueError("period must be >= 1")


def sma(values: Sequence[float], period: int) -> Series:
    _check_period(period)
    out: Series = [None] * len(values)
    if len(values) < period:
        return out
    window = sum(values[:period])
    out[period - 1] = window / period
    for i in range(period, len(values)):
        window += values[i] - values[i - period]
        out[i] = window / period
    return out


def ema(values: Sequence[float], period: int) -> Series:
    _check_period(period)
    out: Series = [None] * len(values)
    if len(values) < period:
        return out
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    alpha = 2.0 / (period + 1)
    for i in range(period, len(values)):
        prev = alpha * values[i] + (1 - alpha) * prev
        out[i] = prev
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def rsi(values: Sequence[float], period: int = 14) -> Series:
    """Relative Strength Index with Wilder smoothing."""
    _check_period(period)
    out: Series = [None] * len(values)
    if len(values) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def macd(
    values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[Series, Series, Series]:
    """Return ``(macd_line, signal_line, histogram)``."""
    if fast >= slow:
        raise ValueError("fast period must be smaller than slow period")
    fast_ema, slow_ema = ema(values, fast), ema(values, slow)
    line: Series = [
        (f - s) if f is not None and s is not None else None
        for f, s in zip(fast_ema, slow_ema, strict=True)
    ]
    signal_line: Series = [None] * len(values)
    first = next((i for i, v in enumerate(line) if v is not None), None)
    if first is not None:
        valid = [v for v in line[first:] if v is not None]
        for offset, value in enumerate(ema(valid, signal)):
            signal_line[first + offset] = value
    hist: Series = [
        (m - s) if m is not None and s is not None else None
        for m, s in zip(line, signal_line, strict=True)
    ]
    return line, signal_line, hist


def bollinger(
    values: Sequence[float], period: int = 20, k: float = 2.0
) -> tuple[Series, Series, Series]:
    """Return ``(middle, upper, lower)`` using population standard deviation."""
    _check_period(period)
    middle: Series = [None] * len(values)
    upper: Series = [None] * len(values)
    lower: Series = [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        mean = sum(window) / period
        std = math.sqrt(sum((x - mean) ** 2 for x in window) / period)
        middle[i], upper[i], lower[i] = mean, mean + k * std, mean - k * std
    return middle, upper, lower


def true_ranges(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]
) -> list[float]:
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("highs, lows and closes must have equal length")
    out: list[float] = []
    for i, (h, low) in enumerate(zip(highs, lows, strict=True)):
        if i == 0:
            out.append(h - low)
        else:
            prev_close = closes[i - 1]
            out.append(max(h - low, abs(h - prev_close), abs(low - prev_close)))
    return out


def atr(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14
) -> Series:
    """Average True Range with Wilder smoothing."""
    _check_period(period)
    trs = true_ranges(highs, lows, closes)
    out: Series = [None] * len(trs)
    if len(trs) < period:
        return out
    prev = sum(trs[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(trs)):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


def swing_points(
    highs: Sequence[float], lows: Sequence[float], window: int = 3
) -> tuple[list[float], list[float]]:
    """Local extrema: a swing high exceeds the ``window`` bars before it and is not exceeded
    by the ``window`` bars after it (mirror logic for swing lows). Returns (highs, lows)."""
    if window < 1:
        raise ValueError("window must be >= 1")
    swing_highs: list[float] = []
    swing_lows: list[float] = []
    for i in range(window, len(highs) - window):
        if highs[i] > max(highs[i - window : i]) and highs[i] >= max(highs[i + 1 : i + window + 1]):
            swing_highs.append(highs[i])
        if lows[i] < min(lows[i - window : i]) and lows[i] <= min(lows[i + 1 : i + window + 1]):
            swing_lows.append(lows[i])
    return swing_highs, swing_lows


def nearest_levels(
    price: float, swing_highs: Sequence[float], swing_lows: Sequence[float]
) -> tuple[float | None, float | None]:
    """Return ``(nearest_support_below, nearest_resistance_above)`` among swing points."""
    supports = [x for x in swing_lows if x < price]
    resistances = [x for x in swing_highs if x > price]
    return (max(supports) if supports else None, min(resistances) if resistances else None)


def realized_volatility(closes: Sequence[float], periods_per_year: int) -> float | None:
    """Annualised standard deviation of log returns (as a fraction, e.g. 0.55 = 55%)."""
    if len(closes) < 3 or any(c <= 0 for c in closes):
        return None
    returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(variance) * math.sqrt(periods_per_year)


def max_drawdown(values: Sequence[float]) -> float | None:
    """Largest peak-to-trough decline as a positive fraction (0.35 = -35%)."""
    if not values:
        return None
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst
