"""Tests for Milestone 3a: Observation artifacts, staleness warnings, and confidence."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
from langchain_core.messages import AIMessage, ToolMessage

from alphamacro.clock import FrozenClock
from alphamacro.config import Settings, StateSettings
from alphamacro.constants import MARKET_TOOL_NAME
from alphamacro.engine import AlphaMacroEngine
from alphamacro.infra.caching import CachingFeed
from alphamacro.infra.http import create_async_client
from alphamacro.infra.state import build_state
from alphamacro.models import (
    ConfidenceLevel,
    Freshness,
    FreshnessPolicy,
    Observation,
    RouteQuery,
    StateBackend,
)
from alphamacro.tools.coingecko import (
    AsyncCoinGeckoClient,
    build_async_market_data_tool,
)


async def test_market_tool_emits_observation_and_downgrades_confidence_on_stale() -> (
    None
):
    clock = FrozenClock(datetime(2026, 6, 1, 12, 0, tzinfo=UTC))
    bundle = build_state(StateSettings(backend=StateBackend.MEMORY), clock=clock)
    policy = FreshnessPolicy(fresh_ttl_s=60, stale_ttl_s=600)
    feed = CachingFeed(
        bundle.cache,
        bundle.locks,
        clock,
        policies=dict.fromkeys(policy.__class__.model_fields, policy)
        if False
        else None,
    )
    fail_upstream = False

    def handler(request: httpx.Request) -> httpx.Response:
        if fail_upstream:
            return httpx.Response(503, text="Service Unavailable")
        payload = [
            {
                "id": "bitcoin",
                "name": "Bitcoin",
                "symbol": "btc",
                "current_price": 98500.0,
                "price_change_percentage_24h": 2.4,
                "high_24h": 99200.0,
                "low_24h": 96100.0,
                "market_cap": 1_940_000_000_000.0,
                "total_volume": 32_000_000_000.0,
                "last_updated": "2026-06-01T11:59:30Z",
            }
        ]
        return httpx.Response(200, content=json.dumps(payload))

    http_client = create_async_client(
        base_url=AsyncCoinGeckoClient.BASE_URL,
        transport=httpx.MockTransport(handler),
    )
    cg = AsyncCoinGeckoClient(
        caching_feed=feed,
        http_client=http_client,
        clock=clock,
    )
    tool = build_async_market_data_tool(cg)

    try:
        # 1. Fresh invocation produces Observation artifact with FRESH status
        fresh_msg = await tool.ainvoke(
            {
                "name": MARKET_TOOL_NAME,
                "args": {"coin_id": "bitcoin"},
                "id": "call-1",
                "type": "tool_call",
            }
        )
        assert isinstance(fresh_msg, ToolMessage)
        assert isinstance(fresh_msg.artifact, Observation)
        assert fresh_msg.artifact.freshness is Freshness.FRESH
        assert fresh_msg.artifact.metrics["price_usd"] == 98500.0

        # 2. Advance past fresh_ttl (60s) with upstream down -> STALE observation
        clock.advance(180.0)
        fail_upstream = True
        stale_msg = await tool.ainvoke(
            {
                "name": MARKET_TOOL_NAME,
                "args": {"coin_id": "bitcoin"},
                "id": "call-2",
                "type": "tool_call",
            }
        )
        await feed.aclose()
        assert isinstance(stale_msg.artifact, Observation)
        assert stale_msg.artifact.freshness is Freshness.STALE
        assert stale_msg.artifact.age_s == 180.0

        # 3. Engine run with STALE observation emits warning and MEDIUM confidence
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
                        stale_msg,
                        AIMessage(
                            content="Harga Bitcoin berada di $98,500 dengan kenaikan 2.4%."
                        ),
                    ]
                }

        engine = AlphaMacroEngine(
            settings,
            llm=object(),
            tools=[tool],
            router=FakeRouter(),
            agent_factory=lambda _c, _d: FakeAgent(),
            clock=clock,
        )
        try:
            res = await engine.arun("Harga BTC sekarang")
            assert len(res.observations) == 1
            assert res.observations[0].freshness is Freshness.STALE
            assert res.confidence is not None
            assert res.confidence.level is ConfidenceLevel.MEDIUM
            assert res.confidence.stale_observations == 1
            assert any("STALE" in w for w in res.warnings)
            md = res.render_markdown()
            assert "## Tingkat Kepercayaan Data" in md
        finally:
            engine.close()
    finally:
        await cg.aclose()
        await http_client.aclose()
