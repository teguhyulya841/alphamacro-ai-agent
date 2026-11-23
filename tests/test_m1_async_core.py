"""Tests for Milestone 1 Part B: LoopThread, batched CoinGecko, and async engine."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage

from alphamacro.clock import FrozenClock
from alphamacro.config import Settings, StateSettings
from alphamacro.engine import AlphaMacroEngine
from alphamacro.infra.caching import CachingFeed
from alphamacro.infra.http import create_async_client
from alphamacro.infra.state import build_state
from alphamacro.infra.sync_bridge import LoopThread
from alphamacro.models import RouteQuery, StateBackend
from alphamacro.tools.coingecko import AsyncCoinGeckoClient


def test_loop_thread_executes_coroutines_and_prevents_reentrancy() -> None:
    bridge = LoopThread(name="test-loop-thread")
    try:

        async def add(a: int, b: int) -> int:
            await asyncio.sleep(0.01)
            return a + b

        assert bridge.run(add(20, 22)) == 42

        async def self_reentrant() -> int:
            return bridge.run(add(1, 2))

        with pytest.raises(RuntimeError, match="cannot be called from its own"):
            bridge.run(self_reentrant())
    finally:
        bridge.close()


async def test_async_coingecko_batched_markets_and_caching() -> None:
    clock = FrozenClock(datetime(2026, 6, 1, 12, 0, tzinfo=UTC))
    bundle = build_state(StateSettings(backend=StateBackend.MEMORY), clock=clock)
    feed = CachingFeed(bundle.cache, bundle.locks, clock)
    requests_made = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests_made
        requests_made += 1
        assert request.url.path == "/api/v3/coins/markets"
        assert request.url.params["ids"] == "bitcoin,ethereum"
        payload = [
            {"id": "bitcoin", "symbol": "btc", "current_price": 95000.0},
            {"id": "ethereum", "symbol": "eth", "current_price": 3400.0},
        ]
        return httpx.Response(200, content=json.dumps(payload))

    transport = httpx.MockTransport(handler)
    http_client = create_async_client(
        base_url=AsyncCoinGeckoClient.BASE_URL,
        transport=transport,
    )
    cg = AsyncCoinGeckoClient(
        caching_feed=feed,
        http_client=http_client,
        clock=clock,
    )
    try:
        first = await cg.get_market_snapshots(["ethereum", "bitcoin"])
        assert requests_made == 1
        assert first["bitcoin"]["current_price"] == 95000.0
        assert first["ethereum"]["current_price"] == 3400.0

        second = await cg.get_market_snapshots(["bitcoin", "ethereum"])
        assert requests_made == 1
        assert second == first
    finally:
        await cg.aclose()
        await http_client.aclose()


async def test_engine_arun_and_sync_run_bridge() -> None:
    settings = Settings(google_api_key="test-google", tavily_api_key="test-tavily")

    class FakeRouter:
        async def aclassify(self, query: str) -> RouteQuery:
            return RouteQuery(category="CRYPTO_MARKET", reasoning="Test async route")

        def classify(self, query: str) -> RouteQuery:
            return RouteQuery(category="CRYPTO_MARKET", reasoning="Test sync route")

    class FakeAgent:
        async def ainvoke(
            self, payload: dict[str, Any], config: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            return {"messages": [AIMessage(content="BTC stabil di $95,000.")]}

    engine = AlphaMacroEngine(
        settings,
        llm=object(),
        tools=[],
        router=FakeRouter(),
        agent_factory=lambda _cat, _dt: FakeAgent(),
    )
    try:
        res_async = await engine.arun("Cek harga BTC", channel="api")
        assert res_async.category == "CRYPTO_MARKET"
        assert "95,000" in res_async.report

        res_sync = engine.run("Cek harga BTC", channel="cli")
        assert res_sync.report == res_async.report
    finally:
        engine.close()
