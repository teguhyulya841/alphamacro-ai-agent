"""Background worker process for scheduled digests and threshold alerts."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal

from alphamacro.api.main import _build_state_settings
from alphamacro.config import Settings
from alphamacro.delivery.telegram import TelegramDeliveryClient
from alphamacro.engine import AlphaMacroEngine
from alphamacro.infra.state import build_state
from alphamacro.logging_config import configure_logging
from alphamacro.models import DigestSchedule
from alphamacro.scheduler import ResearchScheduler

logger = logging.getLogger("alphamacro.worker")


async def run_worker() -> None:
    settings = Settings.from_env()
    configure_logging(
        settings.log_level,
        secrets=settings.secrets(),
        json_logs=settings.log_format == "json",
    )

    state_bundle = build_state(_build_state_settings())
    engine = AlphaMacroEngine(settings, state_bundle=state_bundle)

    if engine.market_client is None:
        raise RuntimeError("AsyncCoinGeckoClient gagal diinisialisasi pada engine.")

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip() or None
    delivery = TelegramDeliveryClient(bot_token) if bot_token else None

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

    digest_query = os.getenv("DIGEST_QUERY", "").strip()
    if digest_query:
        scheduler.upsert_digest(
            DigestSchedule(
                digest_id="scheduled-market-digest",
                query=digest_query,
                interval_s=int(os.getenv("DIGEST_INTERVAL_S", "86400")),
            )
        )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    logger.info("Starting AlphaMacro-Crypto background scheduler worker")
    scheduler.start()
    try:
        await stop_event.wait()
    finally:
        logger.info("Shutting down scheduler worker...")
        await scheduler.stop()
        await engine.aclose()
        if delivery is not None:
            await delivery.aclose()


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
