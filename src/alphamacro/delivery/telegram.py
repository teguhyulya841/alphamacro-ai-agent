"""Async Telegram Bot API delivery client with chunking and secret redaction."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from alphamacro.constants import DISCLAIMER, WIB
from alphamacro.infra.http import create_async_client
from alphamacro.models import AgentResult, AlertEvent, DeliveryReceipt

logger = logging.getLogger(__name__)

_TELEGRAM_MAX_CHARS = 4000


def split_telegram_message(
    text: str, max_len: int = _TELEGRAM_MAX_CHARS
) -> list[str]:
    """Split long reports at newline boundaries to fit Telegram's 4096-char limit."""
    cleaned = text.strip()
    if not cleaned:
        return []
    if len(cleaned) <= max_len:
        return [cleaned]

    chunks: list[str] = []
    remaining = cleaned
    while len(remaining) > max_len:
        split_at = remaining.rfind("\n", 0, max_len)
        if split_at <= 0:
            split_at = remaining.rfind(" ", 0, max_len)
        if split_at <= 0:
            split_at = max_len
        chunk = remaining[:split_at].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def format_alert_message(event: AlertEvent) -> str:
    """Render an AlertEvent with provenance and compliance disclaimer."""
    ts_wib = event.triggered_at.astimezone(WIB).strftime("%Y-%m-%d %H:%M:%S WIB")
    lines = [
        f"*[ALPHA ALERT] {event.coin_id.upper()}*",
        f"Rule: `{event.rule_id}` ({event.operator.value})",
        f"Waktu: {ts_wib}",
        f"Detail: {event.summary}",
    ]
    if event.cross_validation is not None and event.cross_validation.quotes:
        cv = event.cross_validation
        status = "KONSISTEN" if cv.is_consistent else "DIVERGEN"
        lines.append(
            f"Multi-Venue: {status} | Konsensus: ${cv.consensus_price_usd:,.2f} | "
            f"Max Spread: {cv.max_spread_pct:.2f}%"
        )
    lines.extend(["", DISCLAIMER])
    return "\n".join(lines)


def format_digest_message(result: AgentResult) -> str:
    """Render an AgentResult digest report for Telegram delivery."""
    return result.render_markdown(include_header=True)


class TelegramDeliveryClient:
    """Async Telegram Bot API client that never leaks the bot token in errors."""

    BASE_URL = "https://api.telegram.org"

    def __init__(
        self,
        bot_token: str,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout_s: float = 15.0,
        max_retries: int = 2,
    ) -> None:
        token = (bot_token or "").strip()
        if not token:
            raise ValueError("Telegram bot token tidak boleh kosong.")
        self._token = token
        self._max_retries = max_retries
        self._owns_client = http_client is None
        self._client = (
            http_client
            if http_client is not None
            else create_async_client(base_url=self.BASE_URL, timeout_s=timeout_s)
        )

    async def _post_chunk(
        self,
        chat_id: str,
        text: str,
        parse_mode: str | None,
    ) -> None:
        path = f"/bot{self._token}/sendMessage"
        payload: dict[str, Any] = {
            "chat_id": str(chat_id),
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.post(path, json=payload)
                if (
                    response.status_code == 400
                    and parse_mode is not None
                ):
                    # Retry without parse_mode if unescaped Markdown broke formatting
                    fallback_payload = dict(payload)
                    fallback_payload.pop("parse_mode", None)
                    fallback_resp = await self._client.post(
                        path, json=fallback_payload
                    )
                    fallback_resp.raise_for_status()
                    return
                if (
                    response.status_code in {429, 500, 502, 503, 504}
                    and attempt < self._max_retries
                ):
                    await asyncio.sleep(0.3 * (2**attempt))
                    continue
                response.raise_for_status()
                return
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                raise RuntimeError(
                    f"Telegram API mengembalikan HTTP {status}."
                ) from None
            except httpx.RequestError as exc:
                if attempt >= self._max_retries:
                    raise RuntimeError(
                        f"Gagal terhubung ke Telegram API ({type(exc).__name__})."
                    ) from None
                await asyncio.sleep(0.3 * (2**attempt))

    async def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        parse_mode: str | None = "Markdown",
    ) -> DeliveryReceipt:
        """Send text to ``chat_id``, splitting automatically if over 4000 chars."""
        target = str(chat_id).strip()
        if not target:
            return DeliveryReceipt(
                channel="telegram",
                target="",
                delivered=False,
                chunks_sent=0,
                error="chat_id tidak boleh kosong.",
            )
        chunks = split_telegram_message(text)
        if not chunks:
            return DeliveryReceipt(
                channel="telegram",
                target=target,
                delivered=False,
                chunks_sent=0,
                error="Pesan kosong.",
            )

        sent = 0
        try:
            for chunk in chunks:
                await self._post_chunk(target, chunk, parse_mode=parse_mode)
                sent += 1
            return DeliveryReceipt(
                channel="telegram",
                target=target,
                delivered=True,
                chunks_sent=sent,
            )
        except Exception as exc:
            logger.warning(
                "Telegram delivery failed target=%s sent=%d/%d: %s",
                target,
                sent,
                len(chunks),
                exc,
            )
            return DeliveryReceipt(
                channel="telegram",
                target=target,
                delivered=False,
                chunks_sent=sent,
                error=str(exc),
            )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
