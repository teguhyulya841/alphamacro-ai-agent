"""ASGI entrypoint for running the FastAPI server with Uvicorn/Gunicorn."""

from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from alphamacro.api.app import create_app
from alphamacro.config import ApiSettings, Settings, StateSettings
from alphamacro.delivery.telegram import TelegramDeliveryClient
from alphamacro.engine import AlphaMacroEngine
from alphamacro.infra.state import build_state
from alphamacro.logging_config import configure_logging
from alphamacro.models import StateBackend
from alphamacro.scheduler import ResearchScheduler


def _parse_api_key_hashes(raw: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for item in raw.split(","):
        cleaned = item.strip()
        if ":" not in cleaned:
            continue
        name, digest = cleaned.split(":", 1)
        if name.strip() and digest.strip():
            pairs[name.strip()] = digest.strip().lower()
    if not pairs:
        default_hash = hashlib.sha256(b"changeme-dev-key").hexdigest()
        pairs["default"] = default_hash
    return pairs


def _build_state_settings() -> StateSettings:
    backend_raw = os.getenv("STATE_BACKEND", "memory").lower().strip()
    backend = (
        StateBackend(backend_raw)
        if backend_raw in {b.value for b in StateBackend}
        else StateBackend.MEMORY
    )
    kwargs: dict[str, object] = {"backend": backend}
    if "redis_url" in StateSettings.model_fields and os.getenv("REDIS_URL"):
        kwargs["redis_url"] = os.getenv("REDIS_URL")
    if "sqlite_path" in StateSettings.model_fields and os.getenv("SQLITE_PATH"):
        kwargs["sqlite_path"] = os.getenv("SQLITE_PATH")
    return StateSettings(**kwargs)  # type: ignore[arg-type]


def build_production_app() -> FastAPI:
    settings = Settings.from_env()
    configure_logging(
        settings.log_level,
        secrets=settings.secrets(),
        json_logs=settings.log_format == "json",
    )
    state_bundle = build_state(_build_state_settings())
    engine = AlphaMacroEngine(settings, state_bundle=state_bundle)

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip() or None
    delivery = TelegramDeliveryClient(bot_token) if bot_token else None

    if engine.market_client is None:
        raise RuntimeError("market_client is required to initialize ResearchScheduler")

    scheduler = ResearchScheduler(
        engine=engine,
        market_client=engine.market_client,
        delivery=delivery,
        default_chat_id=chat_id,
        venue_validator=engine.venue_validator,
        state_bundle=state_bundle,
        audit=engine.audit,
        poll_interval_s=float(os.getenv("SCHEDULER_POLL_INTERVAL_S", "60")),
    )

    api_settings = ApiSettings(
        api_key_hashes=_parse_api_key_hashes(os.getenv("API_KEY_HASHES", "")),
        rate_limit_per_minute=int(os.getenv("API_RATE_LIMIT_PER_MINUTE", "30")),
        max_concurrent_requests=int(os.getenv("API_MAX_CONCURRENT_REQUESTS", "8")),
    )
    app = create_app(
        engine,
        api_settings,
        market_client=engine.market_client,
        state_bundle=state_bundle,
        scheduler=scheduler,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        await engine.aclose()
        if delivery is not None:
            await delivery.aclose()

    app.router.lifespan_context = lifespan
    return app


app = build_production_app()
