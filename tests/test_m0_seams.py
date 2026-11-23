"""Tests for Milestone 0 seams: Clock, Protocols, Pydantic DTOs, and SecretStr settings."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from alphamacro.clock import FrozenClock, SystemClock
from alphamacro.config import ApiSettings, BotSettings, Settings, StateSettings
from alphamacro.exceptions import ConfigurationError
from alphamacro.models import (
    AgentResult,
    CacheClass,
    CacheEntry,
    Freshness,
    FreshnessPolicy,
    GroundingReport,
    Source,
)
from alphamacro.ports import Clock


def test_frozen_clock_satisfies_protocol_and_advances() -> None:
    start = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    clock = FrozenClock(start)
    assert isinstance(clock, Clock)
    assert clock.now() == start
    advanced = clock.advance(90.0)
    assert advanced == datetime(2026, 6, 1, 12, 1, 30, tzinfo=UTC)


def test_system_clock_returns_aware_utc() -> None:
    clock = SystemClock()
    now = clock.now()
    assert now.tzinfo is not None


def test_cache_entry_freshness_transitions() -> None:
    policy = FreshnessPolicy(fresh_ttl_s=60, stale_ttl_s=300)
    fetched = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    entry = CacheEntry(
        key="amc:v1:markets:bitcoin",
        cache_class=CacheClass.MARKETS,
        value={"price": 95000.0},
        fetched_at=fetched,
        source="coingecko",
    )
    assert entry.freshness(policy, datetime(2026, 6, 1, 0, 0, 45, tzinfo=UTC)) is Freshness.FRESH
    assert entry.freshness(policy, datetime(2026, 6, 1, 0, 2, 0, tzinfo=UTC)) is Freshness.STALE
    assert entry.freshness(policy, datetime(2026, 6, 1, 0, 6, 0, tzinfo=UTC)) is Freshness.EXPIRED


def test_agent_result_pydantic_serialization_roundtrip() -> None:
    result = AgentResult(
        query="Analisis BTC",
        category="CRYPTO_MARKET",
        routing_reason="Pertanyaan pasar kripto",
        analysis_time="2026-06-01 12:00 UTC",
        report="Harga BTC berada di $95,000.",
        sources=[Source(title="CoinGecko", url="https://coingecko.com")],
        grounding=GroundingReport(total=1, verified=1, score=1.0, ungrounded=[]),
    )
    dumped = result.to_dict()
    assert dumped["sources"] == [{"title": "CoinGecko", "url": "https://coingecko.com"}]
    assert dumped["grounding"]["score"] == 1.0


def test_settings_masks_secrets_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "super-secret-google")
    monkeypatch.setenv("TAVILY_API_KEY", "super-secret-tavily")
    settings = Settings.from_env(load_env=False)
    rendered = repr(settings)
    assert "super-secret-google" not in rendered
    assert "super-secret-tavily" not in rendered
    assert settings.google_api_key.get_secret_value() == "super-secret-google"


def test_state_settings_requires_redis_url_when_backend_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STATE_BACKEND", "redis")
    monkeypatch.delenv("STATE_REDIS_URL", raising=False)
    with pytest.raises(ConfigurationError):
        StateSettings.from_env(load_env=False)


def test_bot_and_api_settings_parse_csv_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "101, 202, 303")
    bot_cfg = BotSettings.from_env(load_env=False)
    assert bot_cfg.allowed_user_ids == frozenset({101, 202, 303})

    valid_hash = "a" * 64
    monkeypatch.setenv("API_KEY_HASHES", f"client1:{valid_hash}")
    api_cfg = ApiSettings.from_env(load_env=False)
    assert api_cfg.api_key_hashes == {"client1": valid_hash}
