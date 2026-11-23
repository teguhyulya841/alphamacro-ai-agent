"""Tests for Milestone 3b: Multi-venue spot cross-validation (Binance & Coinbase)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
from langchain_core.messages import AIMessage

from alphamacro.clock import FrozenClock
from alphamacro.config import Settings
from alphamacro.constants import MARKET_TOOL_NAME
from alphamacro.engine import AlphaMacroEngine
from alphamacro.infra.http import create_async_client
from alphamacro.models import ConfidenceLevel, Observation, RouteQuery
from alphamacro.tools.coingecko import (
    AsyncCoinGeckoClient,
    build_async_market_data_tool,
)
from alphamacro.tools.venues import (
    BinanceSpotClient,
    CoinbaseSpotClient,
    MultiVenueValidator,
)


async def test_multi_venue_consistent_quotes_and_consensus() -> None:
    clock = FrozenClock(datetime(2026, 6, 1, 12, 0, tzinfo=UTC))

    def binance_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["symbol"] == "BTCUSDT"
        return httpx.Response(
            200, content=json.dumps({"symbol": "BTCUSDT", "price": "95100.00"})
        )

    def coinbase_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/prices/BTC-USD/spot"
        payload = {"data": {"base": "BTC", "currency": "USD", "amount": "94950.00"}}
        return httpx.Response(200, content=json.dumps(payload))

    b_http = create_async_client(
        base_url=BinanceSpotClient.BASE_URL,
        transport=httpx.MockTransport(binance_handler),
    )
    c_http = create_async_client(
        base_url=CoinbaseSpotClient.BASE_URL,
        transport=httpx.MockTransport(coinbase_handler),
    )
    validator = MultiVenueValidator(
        binance=BinanceSpotClient(http_client=b_http, clock=clock),
        coinbase=CoinbaseSpotClient(http_client=c_http, clock=clock),
        clock=clock,
    )
    try:
        report = await validator.cross_validate("bitcoin", reference_price_usd=95000.0)
        assert report.is_consistent is True
        assert report.divergent_venues == []
        assert report.consensus_price_usd == 95000.0
        assert round(report.max_spread_pct, 3) == 0.105
        assert len(report.quotes) == 2
    finally:
        await validator.aclose()
        await b_http.aclose()
        await c_http.aclose()


async def test_multi_venue_divergence_downgrades_engine_confidence() -> None:
    clock = FrozenClock(datetime(2026, 6, 1, 12, 0, tzinfo=UTC))

    def cg_handler(request: httpx.Request) -> httpx.Response:
        payload = [
            {
                "id": "ethereum",
                "name": "Ethereum",
                "symbol": "eth",
                "current_price": 3000.0,
                "price_change_percentage_24h": 1.2,
                "high_24h": 3050.0,
                "low_24h": 2950.0,
                "market_cap": 360_000_000_000.0,
                "total_volume": 15_000_000_000.0,
                "last_updated": "2026-06-01T12:00:00Z",
            }
        ]
        return httpx.Response(200, content=json.dumps(payload))

    def binance_handler(request: httpx.Request) -> httpx.Response:
        # 5% divergence above CoinGecko reference ($3,150 vs $3,000)
        return httpx.Response(
            200, content=json.dumps({"symbol": "ETHUSDT", "price": "3150.00"})
        )

    def coinbase_handler(request: httpx.Request) -> httpx.Response:
        # Simulate Coinbase temporary 503 outage -> graceful fallback
        return httpx.Response(503, text="Service Unavailable")

    cg_http = create_async_client(
        base_url=AsyncCoinGeckoClient.BASE_URL,
        transport=httpx.MockTransport(cg_handler),
    )
    b_http = create_async_client(
        base_url=BinanceSpotClient.BASE_URL,
        transport=httpx.MockTransport(binance_handler),
    )
    c_http = create_async_client(
        base_url=CoinbaseSpotClient.BASE_URL,
        transport=httpx.MockTransport(coinbase_handler),
    )

    cg = AsyncCoinGeckoClient(http_client=cg_http, cache_ttl=0, clock=clock)
    validator = MultiVenueValidator(
        binance=BinanceSpotClient(http_client=b_http, clock=clock),
        coinbase=CoinbaseSpotClient(http_client=c_http, clock=clock),
        clock=clock,
        max_spread_threshold_pct=1.5,
    )
    market_tool = build_async_market_data_tool(cg, validator=validator)

    try:
        tool_msg = await market_tool.ainvoke(
            {
                "name": MARKET_TOOL_NAME,
                "args": {"coin_id": "ethereum"},
                "id": "call-eth",
                "type": "tool_call",
            }
        )
        assert isinstance(tool_msg.artifact, Observation)
        assert tool_msg.artifact.cross_validation is not None
        assert tool_msg.artifact.cross_validation.is_consistent is False
        assert tool_msg.artifact.cross_validation.divergent_venues == ["binance"]

        settings = Settings(google_api_key="g-key", tavily_api_key="t-key")

        class FakeRouter:
            async def aclassify(self, query: str) -> RouteQuery:
                return RouteQuery(category="CRYPTO_MARKET", reasoning="test")

        class FakeAgent:
            async def ainvoke(
                self, payload: dict[str, Any], config: dict[str, Any] | None = None
            ) -> dict[str, Any]:
                return {
                    "messages": [
                        tool_msg,
                        AIMessage(content="Harga Ethereum saat ini di $3,000 (+1.2%)."),
                    ]
                }

        engine = AlphaMacroEngine(
            settings,
            llm=object(),
            tools=[market_tool],
            router=FakeRouter(),
            agent_factory=lambda _c, _d: FakeAgent(),
            clock=clock,
        )
        try:
            res = await engine.arun("Cek harga ETH")
            assert len(res.cross_validations) == 1
            assert res.cross_validations[0].is_consistent is False
            assert res.confidence is not None
            assert res.confidence.level is ConfidenceLevel.MEDIUM
            assert any("Divergensi harga ethereum" in w for w in res.warnings)
            md = res.render_markdown()
            assert "## Validasi Silang Multi-Venue" in md
            assert "DIVERGEN" in md
        finally:
            engine.close()
    finally:
        await validator.aclose()
        await cg.aclose()
        await cg_http.aclose()
        await b_http.aclose()
        await c_http.aclose()
