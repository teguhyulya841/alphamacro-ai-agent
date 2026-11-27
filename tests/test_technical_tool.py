from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from alphamacro.exceptions import CoinNotFoundError, MarketDataError
from alphamacro.tools.technical import build_technical_tool, fetch_snapshot

if TYPE_CHECKING:
    from alphamacro.tools.coingecko import CoinGeckoClient


class FakeClient:
    def __init__(
        self,
        closes: list[float] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.closes = (
            closes if closes is not None else [100 + i * 0.4 for i in range(260)]
        )
        self.error = error

    def get_daily_closes(self, coin_id: str, days: int = 365) -> list[float]:
        if self.error is not None:
            raise self.error
        return self.closes

    def get_ohlc(self, coin_id: str, days: int = 30) -> list[list[float]]:
        return [[float(i), 100.0, 103.0, 97.0, 100.0 + (i % 6)] for i in range(180)]


def _as_client(fake: FakeClient) -> CoinGeckoClient:
    return cast("CoinGeckoClient", fake)


def test_tool_returns_citable_text() -> None:
    out = build_technical_tool(_as_client(FakeClient())).invoke(
        {"coin_id": "bitcoin", "timeframe": "daily"}
    )
    assert out.startswith("TECHNICAL INDICATORS")
    assert "RSI 14:" in out and "Retrieved At:" in out


def test_tool_4h_includes_atr() -> None:
    out = build_technical_tool(_as_client(FakeClient())).invoke(
        {"coin_id": "ethereum", "timeframe": "4h"}
    )
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
def test_tool_errors_are_returned_not_raised(
    kwargs: dict[str, str], client: FakeClient, needle: str
) -> None:
    out = build_technical_tool(_as_client(client)).invoke(kwargs)
    assert out.startswith("TOOL ERROR") and needle in out


def test_tool_swallows_unexpected_errors_without_leaking() -> None:
    out = build_technical_tool(
        _as_client(FakeClient(error=KeyError("secret")))
    ).invoke({"coin_id": "bitcoin"})
    assert out.startswith("TOOL ERROR") and "secret" not in out


def test_fetch_snapshot_validates_timeframe() -> None:
    with pytest.raises(MarketDataError):
        fetch_snapshot(_as_client(FakeClient()), "bitcoin", "weekly")
