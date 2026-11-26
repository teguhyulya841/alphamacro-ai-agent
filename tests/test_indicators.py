import math

import pytest

from alphamacro.quant import indicators as ind
from alphamacro.quant.snapshot import build_snapshot, format_snapshot

# Widely published worked RSI example (period 14);
# expected first values 70.53, 66.32, 66.55 ...
RSI_CLOSES = [
    44.3389, 44.0902, 44.1497, 43.6124, 44.3278, 44.8264, 45.0955, 45.4245,
    45.8433, 46.0826, 45.8931, 46.0328, 45.6140, 46.2820, 46.2820, 46.0028,
    46.0328, 46.4116, 46.2222, 45.6439, 46.2122, 46.2521, 45.7137, 46.4515,
    45.7835, 45.3548, 44.0288, 44.1783, 44.2184, 44.5382,
]  # fmt: skip


def test_rsi_matches_published_reference():
    values = [round(v, 2) for v in ind.rsi(RSI_CLOSES, 14) if v is not None]
    assert values[:6] == [70.53, 66.32, 66.55, 69.41, 66.36, 57.97]


def test_rsi_extremes_and_flat():
    assert ind.rsi(list(range(1, 40)), 14)[-1] == 100.0
    assert ind.rsi(list(range(40, 1, -1)), 14)[-1] == pytest.approx(0.0)
    assert ind.rsi([5.0] * 40, 14)[-1] == 50.0


def test_series_alignment_and_warmup():
    data = [float(x) for x in range(1, 11)]
    out = ind.sma(data, 3)
    assert len(out) == len(data)
    assert out[:2] == [None, None]
    assert out[2:5] == [2.0, 3.0, 4.0]


def test_ema_seeded_by_sma():
    out = ind.ema([1.0, 2.0, 3.0, 4.0, 5.0], 3)
    assert out[2] == pytest.approx(2.0)  # SMA seed
    assert out[3] == pytest.approx(0.5 * 4 + 0.5 * 2.0)  # alpha = 2/(3+1)
    assert out[4] == pytest.approx(0.5 * 5 + 0.5 * out[3])


def test_short_series_gives_all_none():
    assert ind.sma([1.0, 2.0], 5) == [None, None]
    assert ind.rsi([1.0, 2.0], 14) == [None, None]


def test_macd_constant_series_is_zero_and_histogram_consistent():
    line, sig, hist = ind.macd([10.0] * 80)
    assert line[-1] == pytest.approx(0.0)
    assert sig[-1] == pytest.approx(0.0)
    line, sig, hist = ind.macd([float(i) for i in range(100)])
    assert line[-1] > 0
    assert hist[-1] == pytest.approx(line[-1] - sig[-1])


def test_macd_validates_periods():
    with pytest.raises(ValueError):
        ind.macd([1.0] * 50, fast=26, slow=12)


def test_bollinger_constant_has_zero_width_and_known_std():
    mid, up, low = ind.bollinger([5.0] * 25, 20)
    assert up[-1] == low[-1] == mid[-1] == 5.0
    mid, up, low = ind.bollinger([1.0, 2.0, 3.0, 4.0, 5.0], 5, k=2)
    assert mid[-1] == 3.0
    assert up[-1] == pytest.approx(3.0 + 2 * math.sqrt(2.0))  # population std of 1..5


def test_atr_constant_range():
    highs, lows, closes = [11.0] * 30, [9.0] * 30, [10.0] * 30
    assert ind.atr(highs, lows, closes, 14)[-1] == pytest.approx(2.0)


def test_true_range_uses_gaps():
    tr = ind.true_ranges([10, 15], [9, 14], [9.5, 14.5])
    assert tr[1] == pytest.approx(5.5)  # |15 - 9.5| beats high-low


def test_swing_points_and_nearest_levels():
    highs = [1, 2, 3, 9, 3, 2, 1, 2, 3, 6, 3, 2, 1]
    lows = [h - 0.5 for h in highs]
    swing_highs, swing_lows = ind.swing_points(highs, lows, window=2)
    assert 9 in swing_highs and 6 in swing_highs
    support, resistance = ind.nearest_levels(5.0, swing_highs, swing_lows)
    assert resistance == 6
    assert support is not None and support < 5.0


def test_volatility_and_drawdown():
    assert ind.max_drawdown([100, 120, 60, 90]) == pytest.approx(0.5)
    assert ind.max_drawdown([1, 2, 3]) == 0.0
    assert ind.realized_volatility([100.0] * 30, 365) == pytest.approx(0.0)
    assert ind.realized_volatility([1.0, 2.0], 365) is None
    assert ind.realized_volatility([1.0, -1.0, 2.0], 365) is None


def test_snapshot_needs_enough_data():
    with pytest.raises(ValueError):
        build_snapshot("bitcoin", "daily", [1.0] * 10)


def test_snapshot_daily_and_text():
    closes = [100 + i * 0.5 + (3 if i % 7 == 0 else 0) for i in range(260)]
    snap = build_snapshot("bitcoin", "daily", closes)
    assert snap["sma200"] is not None and snap["atr14"] is None
    assert snap["price"] == closes[-1]
    assert "di atas SMA50 dan di atas SMA200" in snap["trend"]
    text = format_snapshot(snap, "t")
    assert "RSI 14:" in text and "Retrieved At: t" in text and "not predictions" in text


def test_snapshot_4h_has_atr_and_levels():
    closes = [100 + 5 * math.sin(i / 6) for i in range(180)]
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    snap = build_snapshot("ethereum", "4h", closes, highs, lows)
    assert snap["sma200"] is None  # only 180 candles
    assert snap["atr14"] == pytest.approx(2.0, abs=0.5)
    assert snap["nearest_support"] is not None or snap["nearest_resistance"] is not None
