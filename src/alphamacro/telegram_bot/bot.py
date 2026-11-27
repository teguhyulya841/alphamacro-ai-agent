"""Bot wiring and process entry point."""

from __future__ import annotations

import logging
import signal
import sys
from types import FrameType

import telebot
from telebot import apihelper
from telebot.types import BotCommand

from alphamacro import __version__
from alphamacro.config import BotSettings, Settings
from alphamacro.engine import AlphaMacroEngine
from alphamacro.exceptions import ConfigurationError
from alphamacro.logging_config import configure_logging
from alphamacro.telegram_bot.handlers import BotController

logger = logging.getLogger("alphamacro.bot")


def build_bot(engine: AlphaMacroEngine, settings: BotSettings) -> telebot.TeleBot:
    """Create a TeleBot with all handlers registered."""
    bot = telebot.TeleBot(
        settings.telegram_token.get_secret_value(),
        threaded=True,
        num_threads=settings.max_concurrent_jobs + 2,  # spare threads for quick replies
    )
    BotController(
        bot,
        engine,
        settings,
        max_query_chars=engine.settings.max_query_chars,
    ).register()
    return bot


def main() -> int:
    try:
        settings = Settings.from_env()
        bot_settings = BotSettings.from_env()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    configure_logging(
        settings.log_level,
        secrets=(*settings.secrets(), *bot_settings.secrets()),
        json_logs=settings.log_format == "json",
    )

    # More forgiving network timeouts (slow/unstable links to api.telegram.org).
    apihelper.CONNECT_TIMEOUT = 20
    apihelper.READ_TIMEOUT = 45

    engine = AlphaMacroEngine(settings)
    bot = build_bot(engine, bot_settings)

    try:
        bot.set_my_commands(
            [
                BotCommand("start", "Mulai"),
                BotCommand("help", "Bantuan"),
                BotCommand("about", "Tentang bot & privasi"),
            ]
        )
    except Exception as exc:
        logger.warning(
            "Could not register bot commands (%s); continuing", type(exc).__name__
        )

    def _shutdown(signum: int, _frame: FrameType | None) -> None:
        logger.info("Received signal %s, shutting down", signum)
        bot.stop_polling()

    signal.signal(signal.SIGTERM, _shutdown)

    restricted = "restricted" if bot_settings.allowed_user_ids else "public"
    logger.info("AlphaMacro-Crypto bot v%s online (%s access)", __version__, restricted)
    try:
        # Request timeout must be well above the server-side long-poll time.
        bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=15)
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    finally:
        bot.stop_polling()
        engine.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
