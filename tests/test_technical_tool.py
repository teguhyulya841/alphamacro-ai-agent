import pytest

from alphamacro.exceptions import CoinNotFoundError, MarketDataError
from alphamacro.tools.technical import build_technical_tool, fetch_snapshot


class FakeClient:
    def __init__(self, closes=None, error=None):
        self.closes = closes if closes is not None else [100 + i * 0.4 for i in range(260)]
        self.error = error

    def get_daily_closes(self, coin_id, days=365):
        if self.error:
            raise self.error
        return self.closes

    def get_ohlc(self, coin_id, days=30):
        return [[i, 100, 103, 97, 100 + (i % 6)] for i in range(180)]


def test_tool_returns_citable_text():
    out = build_technical_tool(FakeClient()).invoke({"coin_id": "bitcoin", "timeframe": "daily"})
    assert out.startswith("TECHNICAL INDICATORS")
    assert "RSI 14:" in out and "Retrieved At:" in out


def test_tool_4h_includes_atr():
    out = build_technical_tool(FakeClient()).invoke({"coin_id": "ethereum", "timeframe": "4h"})
    assert "ATR 14: $" in out


@pytest.mark.parametrize(
    ("kwargs", "client", "needle"),
    [
        ({"coin_id": "bitcoin", "timeframe": "1w"}, FakeClient(), "timeframe"),
        ({"coin_id": "../x", "timeframe": "daily"}, FakeClient(), "tidak valid"),
        ({"coin_id": "bitcoin"}, FakeClient(closes=[1.0] * 5), "tidak cukup"),
        ({"coin_id": "bitcoin"}, FakeClient(error=MarketDataError("down")), "down"),
        ({"coin_id": "zzz"}, FakeClient(error=CoinNotFoundError("nf")), "nf"),
    ],
)
def test_tool_errors_are_returned_not_raised(kwargs, client, needle):
    out = build_technical_tool(client).invoke(kwargs)
    assert out.startswith("TOOL ERROR") and needle in out


def test_tool_swallows_unexpected_errors_without_leaking():
    out = build_technical_tool(FakeClient(error=KeyError("secret"))).invoke({"coin_id": "bitcoin"})
    assert out.startswith("TOOL ERROR") and "secret" not in out


def test_fetch_snapshot_validates_timeframe():
    with pytest.raises(MarketDataError):
        fetch_snapshot(FakeClient(), "bitcoin", "weekly")
