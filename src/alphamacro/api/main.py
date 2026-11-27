"""ASGI entrypoint for running the FastAPI server with Uvicorn/Gunicorn."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI

from alphamacro.api.app import create_app
from alphamacro.config import ApiSettings, Settings, StateSettings, WorkerSettings
from alphamacro.delivery.telegram import TelegramDeliveryClient
from alphamacro.engine import AlphaMacroEngine
from alphamacro.infra.state import build_state
from alphamacro.logging_config import configure_logging
from alphamacro.scheduler import ResearchScheduler

if TYPE_CHECKING:
    app: FastAPI


def build_production_app() -> FastAPI:
    """Build the fail-closed production FastAPI application from env settings."""
    settings = Settings.from_env()
    state_settings = StateSettings.from_env()
    api_settings = ApiSettings.from_env()
    worker_settings = WorkerSettings.from_env()

    configure_logging(
        settings.log_level,
        secrets=(
            *settings.secrets(),
            *state_settings.secrets(),
            *worker_settings.secrets(),
        ),
        json_logs=settings.log_format == "json",
    )

    state_bundle = build_state(state_settings)
    engine = AlphaMacroEngine(settings, state_bundle=state_bundle)

    if engine.market_client is None:
        raise RuntimeError("market_client is required to initialize ResearchScheduler")

    bot_token = (
        worker_settings.telegram_token.get_secret_value()
        if worker_settings.telegram_token is not None
        else ""
    )
    delivery = TelegramDeliveryClient(bot_token) if bot_token else None

    scheduler = ResearchScheduler(
        engine=engine,
        market_client=engine.market_client,
        delivery=delivery,
        default_chat_id=worker_settings.telegram_chat_id,
        venue_validator=engine.venue_validator,
        state_bundle=state_bundle,
        audit=engine.audit,
        poll_interval_s=worker_settings.scheduler_poll_interval_s,
    )

    fast_app = create_app(
        engine,
        api_settings,
        market_client=engine.market_client,
        state_bundle=state_bundle,
        scheduler=scheduler,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await engine.aclose()
            if delivery is not None:
                await delivery.aclose()
            await state_bundle.aclose()

    fast_app.router.lifespan_context = lifespan
    return fast_app


def __getattr__(name: str) -> FastAPI:
    if name == "app":
        return build_production_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
