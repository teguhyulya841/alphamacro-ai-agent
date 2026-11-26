"""Telegram message handling, kept separate from wiring so it is unit-testable."""

from __future__ import annotations

import html
import logging
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

import requests
from telebot.apihelper import ApiTelegramException
from telebot.types import LinkPreviewOptions

from alphamacro import __version__
from alphamacro.config import BotSettings
from alphamacro.exceptions import InvalidQueryError
from alphamacro.telegram_bot.formatting import markdown_to_telegram_html, split_message
from alphamacro.telegram_bot.throttle import (
    ActiveUserRegistry,
    SlidingWindowRateLimiter,
)

logger = logging.getLogger(__name__)

_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
_TRANSIENT_NETWORK_ERRORS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
)

WELCOME_HTML = (
    "\U0001f916 <b>AlphaMacro-Crypto Agent</b>\n"
    "<i>Alpha Intelligence Mentor</i> aktif.\n\n"
    "Kirim topik makro atau aset kripto yang ingin dianalisis.\n"
    "Contoh: <code>Analisa BTC dan DXY hari ini</code>\n\n"
    "Perintah: /help, /about\n\n"
    "\u26a0\ufe0f <i>Informasi edukatif, bukan nasihat investasi.</i>"
)

ABOUT_HTML = (
    f"<b>AlphaMacro-Crypto</b> v{__version__}\n"
    "Riset makroekonomi &amp; kripto berbasis AI"
    "(router + agent + data CoinGecko &amp; web).\n\n"
    "<b>Privasi:</b> isi pesan Anda tidak disimpan oleh bot ini, tetapi diteruskan ke "
    "penyedia AI dan pencarian web (Tavily) untuk menghasilkan jawaban.\n\n"
    "<b>Disclaimer:</b> Laporan dihasilkan AI, dapat mengandung kesalahan, dan bukan "
    "nasihat investasi. Aset kripto berisiko tinggi."
)

UNSUPPORTED_CONTENT_TYPES = [
    "photo", "document", "voice", "video", "audio", "sticker",
    "location", "contact", "video_note", "animation",
]  # fmt: skip


class TypingIndicator:
    """Keep Telegram's 'typing...' status alive during long-running work."""

    def __init__(self, bot: Any, chat_id: int, interval: float = 4.0) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _send(self) -> None:
        try:
            self._bot.send_chat_action(self._chat_id, "typing")
        except Exception:
            logger.debug("send_chat_action failed", exc_info=True)

    def _run(self) -> None:
        self._send()
        while not self._stop.wait(self._interval):
            self._send()

    def __enter__(self) -> TypingIndicator:
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)


class BotController:
    """Business logic for the bot: auth, throttling, execution, safe error reporting."""

    def __init__(
        self,
        bot: Any,
        engine: Any,
        settings: BotSettings,
        *,
        max_query_chars: int,
        limiter: SlidingWindowRateLimiter | None = None,
        active_users: ActiveUserRegistry | None = None,
        sleep: Callable[[float], None] = time.sleep,
        retry_delays: tuple[float, ...] = (2.0, 5.0),
    ) -> None:
        self._bot = bot
        self._engine = engine
        self._settings = settings
        self._max_query_chars = max_query_chars
        self._limiter = limiter or SlidingWindowRateLimiter(
            settings.rate_limit_per_minute
        )
        self._active = active_users or ActiveUserRegistry()
        self._jobs = threading.BoundedSemaphore(settings.max_concurrent_jobs)
        self._sleep = sleep
        self._retry_delays = retry_delays

    # -- registration -----------------------------------------------------

    def register(self) -> None:
        bot = self._bot
        bot.message_handler(commands=["start", "help"])(self.on_start)
        bot.message_handler(commands=["about"])(self.on_about)
        bot.message_handler(
            content_types=["text"], func=lambda m: (m.text or "").startswith("/")
        )(self.on_unknown_command)
        bot.message_handler(content_types=["text"])(self.on_text)
        bot.message_handler(content_types=UNSUPPORTED_CONTENT_TYPES)(
            self.on_unsupported
        )

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _user_id(message: Any) -> int:
        user = getattr(message, "from_user", None)
        return int(user.id) if user else int(message.chat.id)

    @staticmethod
    def _is_private(message: Any) -> bool:
        return getattr(message.chat, "type", "private") == "private"

    def _is_authorized(self, user_id: int) -> bool:
        allowed = self._settings.allowed_user_ids
        return not allowed or user_id in allowed

    def _with_retry(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Retry transient network failures talking to Telegram (resets, timeouts)."""
        for attempt, delay in enumerate((*self._retry_delays, None)):
            try:
                return func(*args, **kwargs)
            except _TRANSIENT_NETWORK_ERRORS as exc:
                if delay is None:
                    raise
                logger.warning(
                    "Telegram network error (%s); retry %d/%d in %.0fs",
                    type(exc).__name__,
                    attempt + 1,
                    len(self._retry_delays),
                    delay,
                )
                self._sleep(delay)

    def _send_html(self, chat_id: int, text: str) -> None:
        self._with_retry(
            self._bot.send_message,
            chat_id,
            text,
            parse_mode="HTML",
            link_preview_options=_NO_PREVIEW,
        )

    def _send_markdown(self, chat_id: int, markdown_chunk: str) -> None:
        """Send as HTML; if Telegram rejects the markup, resend as plain text."""
        try:
            self._send_html(chat_id, markdown_to_telegram_html(markdown_chunk))
        except ApiTelegramException as exc:
            if exc.error_code != 400:
                raise
            logger.warning(
                "Telegram rejected HTML (%s); sending plain text", exc.description
            )
            self._with_retry(
                self._bot.send_message,
                chat_id,
                markdown_chunk,
                link_preview_options=_NO_PREVIEW,
            )

    def _safe_delete(self, chat_id: int, message_id: int | None) -> None:
        if message_id is None:
            return
        try:
            self._bot.delete_message(chat_id, message_id)
        except Exception:
            logger.debug("delete_message failed", exc_info=True)

    # -- handlers ---------------------------------------------------------

    def on_start(self, message: Any) -> None:
        if self._is_private(message):
            self._send_html(message.chat.id, WELCOME_HTML)

    def on_about(self, message: Any) -> None:
        if self._is_private(message):
            self._send_html(message.chat.id, ABOUT_HTML)

    def on_unknown_command(self, message: Any) -> None:
        if self._is_private(message):
            self._send_html(message.chat.id, "Perintah tidak dikenal. Gunakan /help.")

    def on_unsupported(self, message: Any) -> None:
        if self._is_private(message):
            self._send_html(message.chat.id, "Saat ini bot hanya mendukung pesan teks.")

    def on_text(self, message: Any) -> None:
        if not self._is_private(message):
            return  # groups are intentionally ignored (cost control, avoids spam)

        chat_id = message.chat.id
        user_id = self._user_id(message)

        if not self._is_authorized(user_id):
            logger.warning("Unauthorized access attempt user_id=%s", user_id)
            self._send_html(chat_id, "\u26d4 Anda tidak memiliki akses ke bot ini.")
            return

        query = (message.text or "").strip()
        if not query:
            self._send_html(chat_id, "Silakan kirim pertanyaan dalam bentuk teks.")
            return
        if len(query) > self._max_query_chars:
            self._send_html(
                chat_id,
                f"Pertanyaan terlalu panjang "
                f"(maksimal {self._max_query_chars} karakter).",
            )
            return

        if not self._active.try_acquire(user_id):
            self._send_html(
                chat_id, "\u23f3 Permintaan sebelumnya masih diproses. Mohon tunggu."
            )
            return
        try:
            allowed, retry_after = self._limiter.check(user_id)
            if not allowed:
                self._send_html(
                    chat_id,
                    "\u26a0\ufe0f Terlalu banyak permintaan. "
                    f"Coba lagi dalam {int(retry_after) + 1} detik.",
                )
                return
            if not self._jobs.acquire(blocking=False):
                self._send_html(
                    chat_id, "Server sedang sibuk. Coba lagi sebentar lagi."
                )
                return
            try:
                self._process(chat_id, user_id, query)
            finally:
                self._jobs.release()
        finally:
            self._active.release(user_id)

    def _process(self, chat_id: int, user_id: int, query: str) -> None:
        # Never log message content: only metadata.
        logger.info("Processing request user_id=%s length=%d", user_id, len(query))

        placeholder_id: int | None = None
        try:
            placeholder = self._bot.send_message(
                chat_id,
                "\u23f3 <i>Menganalisis data makro &amp; pasar terkini...</i>",
                parse_mode="HTML",
            )
            placeholder_id = placeholder.message_id
        except Exception:
            logger.debug("Could not send placeholder", exc_info=True)

        result = None
        error_html: str | None = None
        with TypingIndicator(self._bot, chat_id):
            try:
                result = self._engine.run(query, user_id=user_id, channel="telegram")
            except InvalidQueryError as exc:
                error_html = f"\u26a0\ufe0f {html.escape(str(exc))}"
            except Exception:
                error_id = uuid.uuid4().hex[:8]
                logger.exception(
                    "Request failed error_id=%s user_id=%s", error_id, user_id
                )
                error_html = (
                    "\u26a0\ufe0f Terjadi kesalahan saat memproses permintaan. "
                    f"Silakan coba lagi nanti.\nKode: <code>{error_id}</code>"
                )

        self._safe_delete(chat_id, placeholder_id)

        if error_html is not None or result is None:
            self._send_html(chat_id, error_html or "AI tidak menghasilkan jawaban.")
            return

        try:
            for chunk in split_message(result.render_markdown(include_header=False)):
                self._send_markdown(chat_id, chunk)
                self._sleep(0.5)  # stay under Telegram's per-chat flood limits
        except _TRANSIENT_NETWORK_ERRORS as exc:
            logger.error(
                "Report could not be delivered: Telegram unreachable (%s). "
                "Check your network/VPN/DNS.",
                type(exc).__name__,
            )
        except Exception:
            logger.exception("Failed to deliver report to chat")
