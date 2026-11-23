"""Shared fixtures."""

from types import SimpleNamespace

import pytest

from alphamacro.config import Settings

ENV_KEYS = [
    "GOOGLE_API_KEY", "TAVILY_API_KEY", "TELEGRAM_TOKEN", "GEMINI_MODEL",
    "COINGECKO_API_KEY", "LLM_TIMEOUT", "LLM_MAX_RETRIES", "HTTP_TIMEOUT",
    "CACHE_TTL_SECONDS", "MAX_QUERY_CHARS", "AGENT_RECURSION_LIMIT", "LOG_LEVEL",
    "LOG_FORMAT", "TELEGRAM_ALLOWED_USER_IDS", "BOT_RATE_LIMIT_PER_MINUTE",
    "BOT_MAX_CONCURRENT_JOBS",
]  # fmt: skip


@pytest.fixture
def clean_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


@pytest.fixture
def settings():
    return Settings(google_api_key="g" * 12, tavily_api_key="t" * 12)


def make_message(type_, content="", name=None):
    return SimpleNamespace(type=type_, content=content, name=name)
