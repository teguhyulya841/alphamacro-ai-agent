"""Background worker process for scheduled digests and threshold alerts."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys

from alphamacro.config import Settings, StateSettings, WorkerSettings
from alphamacro.delivery.telegram import TelegramDeliveryClient
from alphamacro.engine import AlphaMacroEngine
from alphamacro.exceptions import ConfigurationError
from alphamacro.infra.state import build_state
from alphamacro.logging_config import configure_logging
from alphamacro.models import DigestSchedule
from alphamacro.scheduler import ResearchScheduler

logger = logging.getLogger("alphamacro.worker")


async def run_worker() -> None:
    settings = Settings.from_env()
    state_settings = StateSettings.from_env()
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
        raise RuntimeError("AsyncCoinGeckoClient gagal diinisialisasi pada engine.")

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

    if worker_settings.digest_query:
        scheduler.upsert_digest(
            DigestSchedule(
                digest_id="scheduled-market-digest",
                query=worker_settings.digest_query,
                interval_s=worker_settings.digest_interval_s,
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
        await state_bundle.aclose()


def main() -> int:
    try:
        asyncio.run(run_worker())
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
