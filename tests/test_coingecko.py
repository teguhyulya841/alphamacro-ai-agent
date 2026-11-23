from types import SimpleNamespace

import pytest
import requests

from alphamacro.exceptions import MarketDataError
from alphamacro.tools.coingecko import CoinGeckoClient, format_market_snapshot


class FakeResponse:
    def __init__(self, payload=None, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(
                response=SimpleNamespace(status_code=self.status_code)
            )

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def client_with(responses, ttl=60):
    client = CoinGeckoClient(cache_ttl=ttl)
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    client._session.get = fake_get  # type: ignore[method-assign]
    return client, calls


@pytest.mark.parametrize(
    "bad", ["", "../etc/passwd", "btc?x=1", "a/b", "-bitcoin", "x" * 70]
)
def test_coin_id_invalid(bad):
    with pytest.raises(MarketDataError):
        CoinGeckoClient.normalize_coin_id(bad)


def test_coin_id_normalized():
    assert CoinGeckoClient.normalize_coin_id("  Bitcoin ") == "bitcoin"


def test_success_and_cache():
    client, calls = client_with([FakeResponse([{"id": "bitcoin", "current_price": 1}])])
    assert client.get_market_snapshot("bitcoin")["id"] == "bitcoin"
    assert client.get_market_snapshot("Bitcoin")["id"] == "bitcoin"
    assert len(calls) == 1  # second call served from cache


def test_cache_disabled():
    client, calls = client_with(
        [FakeResponse([{"id": "b"}]), FakeResponse([{"id": "b"}])], ttl=0
    )
    client.get_market_snapshot("b")
    client.get_market_snapshot("b")
    assert len(calls) == 2


def test_unknown_coin():
    client, _ = client_with([FakeResponse([])])
    with pytest.raises(MarketDataError, match="tidak ditemukan"):
        client.get_market_snapshot("nonexistent")


@pytest.mark.parametrize(
    ("raised", "text"),
    [
        (requests.exceptions.Timeout(), "timeout"),
        (requests.exceptions.RetryError(), "rate limit"),
        (requests.exceptions.ConnectionError(), "terhubung"),
    ],
)
def test_network_errors_mapped(raised, text):
    client, _ = client_with([raised])
    with pytest.raises(MarketDataError, match=text):
        client.get_market_snapshot("bitcoin")


def test_http_error_mapped():
    client, _ = client_with([FakeResponse(status=500)])
    with pytest.raises(MarketDataError, match="500"):
        client.get_market_snapshot("bitcoin")


def test_http_404_means_coin_not_found():
    from alphamacro.exceptions import CoinNotFoundError

    client, _ = client_with([FakeResponse(status=404)])
    with pytest.raises(CoinNotFoundError):
        client.get_market_snapshot("bitcoin")


def test_daily_closes_parsed():
    payload = {"prices": [[1, 100.0], [2, 101.5]]}
    client, _ = client_with([FakeResponse(payload)])
    assert client.get_daily_closes("bitcoin") == [100.0, 101.5]


def test_ohlc_parsed_and_validated():
    client, _ = client_with([FakeResponse([[1, 1, 2, 0.5, 1.5], [2, 1.5, 2, 1, 1.8]])])
    assert client.get_ohlc("bitcoin")[0] == [1.0, 1.0, 2.0, 0.5, 1.5]
    bad, _ = client_with([FakeResponse([[1, 2, 3]])])
    with pytest.raises(MarketDataError):
        bad.get_ohlc("bitcoin")


def test_invalid_json_mapped():
    client, _ = client_with([FakeResponse(ValueError("bad"))])
    with pytest.raises(MarketDataError, match="JSON"):
        client.get_market_snapshot("bitcoin")


def test_small_price_precision_and_missing_values():
    out = format_market_snapshot(
        {"id": "x", "name": "X", "current_price": 0.000123}, "t"
    )
    assert "$0.000123" in out
    assert "Market Cap: N/A" in out
