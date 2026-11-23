"""Tests for Milestone 4: Alert & Digest Scheduler + Telegram Delivery."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi.testclient import TestClient

from alphamacro.api.app import create_app
from alphamacro.api.auth import hash_key
from alphamacro.clock import FrozenClock
from alphamacro.config import ApiSettings, StateSettings
from alphamacro.constants import DISCLAIMER
from alphamacro.delivery.telegram import TelegramDeliveryClient
from alphamacro.infra.http import create_async_client
from alphamacro.infra.state import build_state
from alphamacro.models import (
    AgentResult,
    AlertOperator,
    AlertRule,
    DigestSchedule,
    StateBackend,
)
from alphamacro.scheduler import ResearchScheduler
from alphamacro.tools.coingecko import AsyncCoinGeckoClient


async def test_telegram_delivery_chunks_long_messages_and_redacts_token() -> None:
    sent_payloads: list[dict[str, Any]] = []
    secret_token = "123456:SUPER_SECRET_BOT_TOKEN"

    def handler(request: httpx.Request) -> httpx.Response:
        assert secret_token in str(request.url)
        body = json.loads(request.content.decode("utf-8"))
        sent_payloads.append(body)
        return httpx.Response(200, content=json.dumps({"ok": True}))

    http_client = create_async_client(
        base_url=TelegramDeliveryClient.BASE_URL,
        transport=httpx.MockTransport(handler),
    )
    tg = TelegramDeliveryClient(secret_token, http_client=http_client)
    try:
        long_report = ("Baris analisis pasar kripto harian.\n" * 200).strip()
        receipt = await tg.send_message("99887766", long_report)
        assert receipt.delivered is True
        assert receipt.chunks_sent >= 2
        assert len(sent_payloads) == receipt.chunks_sent
        assert all(len(p["text"]) <= 4000 for p in sent_payloads)
    finally:
        await tg.aclose()
        await http_client.aclose()

    # Verify HTTP error never leaks the bot token
    def fail_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Unauthorized")

    fail_http = create_async_client(
        base_url=TelegramDeliveryClient.BASE_URL,
        transport=httpx.MockTransport(fail_handler),
    )
    tg_fail = TelegramDeliveryClient(secret_token, http_client=fail_http, max_retries=0)
    try:
        failed_receipt = await tg_fail.send_message("99887766", "Test alert")
        assert failed_receipt.delivered is False
        assert failed_receipt.error is not None
        assert secret_token not in failed_receipt.error
    finally:
        await tg_fail.aclose()
        await fail_http.aclose()


async def test_scheduler_alerts_cooldown_and_digest_delivery() -> None:
    clock = FrozenClock(datetime(2026, 11, 24, 2, 0, tzinfo=UTC))
    bundle = build_state(StateSettings(backend=StateBackend.MEMORY), clock=clock)
    telegram_messages: list[str] = []

    def cg_handler(request: httpx.Request) -> httpx.Response:
        payload = [
            {
                "id": "bitcoin",
                "symbol": "btc",
                "current_price": 102500.0,
                "price_change_percentage_24h": 4.2,
            }
        ]
        return httpx.Response(200, content=json.dumps(payload))

    def tg_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        telegram_messages.append(body["text"])
        return httpx.Response(200, content=json.dumps({"ok": True}))

    cg_http = create_async_client(
        base_url=AsyncCoinGeckoClient.BASE_URL,
        transport=httpx.MockTransport(cg_handler),
    )
    tg_http = create_async_client(
        base_url=TelegramDeliveryClient.BASE_URL,
        transport=httpx.MockTransport(tg_handler),
    )
    cg = AsyncCoinGeckoClient(http_client=cg_http, cache_ttl=0, clock=clock)
    tg = TelegramDeliveryClient("999:TEST_TOKEN", http_client=tg_http)

    class FakeEngine:
        async def arun(
            self,
            query: str,
            *,
            user_id: int | str | None = None,
            channel: str = "cli",
            request_id: str | None = None,
        ) -> AgentResult:
            return AgentResult(
                query=query,
                category="FULL_RESEARCH",
                routing_reason="digest",
                analysis_time="2026-11-24 09:00 WIB",
                report="Ringkasan makro & BTC bertahan di atas $102,500.",
                tools_used=["get_crypto_market_data"],
                request_id="digest-1",
                model="gemini-2.5-flash",
            )

    scheduler = ResearchScheduler(
        engine=FakeEngine(),
        market_client=cg,
        delivery=tg,
        default_chat_id="123456789",
        state_bundle=bundle,
        clock=clock,
    )
    scheduler.upsert_rule(
        AlertRule(
            rule_id="btc-100k",
            coin_id="bitcoin",
            operator=AlertOperator.PRICE_GT,
            threshold=100000.0,
            cooldown_s=3600,
        )
    )
    scheduler.upsert_digest(
        DigestSchedule(
            digest_id="morning-brief",
            query="Buat ringkasan pasar BTC dan makro hari ini",
            interval_s=86400,
        )
    )

    try:
        # 1. First tick triggers alert + digest
        await scheduler.tick_once()
        assert len(telegram_messages) == 2
        assert "ALPHA ALERT — BITCOIN" in telegram_messages[0]
        assert DISCLAIMER.strip("*") in telegram_messages[0]
        assert "ALPHA INTELLIGENCE BRIEF" in telegram_messages[1]

        # 2. Second tick 10 minutes later suppresses both due to cooldown & interval
        clock.advance(600.0)
        await scheduler.tick_once()
        assert len(telegram_messages) == 2

        # 3. Advance past 3600s cooldown -> alert re-fires, digest still waiting
        clock.advance(3100.0)
        await scheduler.tick_once()
        assert len(telegram_messages) == 3
    finally:
        await cg.aclose()
        await tg.aclose()
        await cg_http.aclose()
        await tg_http.aclose()


def test_alerts_api_crud_and_on_demand_check() -> None:
    clock = FrozenClock(datetime(2026, 11, 24, 2, 0, tzinfo=UTC))

    def cg_handler(request: httpx.Request) -> httpx.Response:
        payload = [
            {
                "id": "solana",
                "symbol": "sol",
                "current_price": 240.0,
                "price_change_percentage_24h": 8.5,
            }
        ]
        return httpx.Response(200, content=json.dumps(payload))

    cg_http = create_async_client(
        base_url=AsyncCoinGeckoClient.BASE_URL,
        transport=httpx.MockTransport(cg_handler),
    )
    cg = AsyncCoinGeckoClient(http_client=cg_http, cache_ttl=0, clock=clock)
    scheduler = ResearchScheduler(
        engine=object(),
        market_client=cg,
        clock=clock,
    )
    api_settings = ApiSettings(api_key_hashes={"admin": hash_key("secret-api-key")})
    app = create_app(object(), api_settings, scheduler=scheduler)
    headers = {"X-API-Key": "secret-api-key"}

    with TestClient(app) as client:
        create_resp = client.post(
            "/v1/alerts",
            headers=headers,
            json={
                "rule_id": "sol-breakout",
                "coin_id": "solana",
                "operator": "price_gt",
                "threshold": 220.0,
                "cooldown_s": 1800,
            },
        )
        assert create_resp.status_code == 200
        assert create_resp.json()["rule_id"] == "sol-breakout"

        list_resp = client.get("/v1/alerts", headers=headers)
        assert list_resp.status_code == 200
        assert len(list_resp.json()) == 1

        check_resp = client.post("/v1/alerts/check", headers=headers)
        assert check_resp.status_code == 200
        events = check_resp.json()
        assert len(events) == 1
        assert events[0]["coin_id"] == "solana"
        assert events[0]["observed_value"] == 240.0

        del_resp = client.delete("/v1/alerts/sol-breakout", headers=headers)
        assert del_resp.status_code == 200
        assert del_resp.json()["deleted"] is True
