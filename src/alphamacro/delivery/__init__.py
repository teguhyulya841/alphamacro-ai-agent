"""Outbound delivery adapters (Telegram Bot API, webhooks, digests)."""

from alphamacro.delivery.telegram import (
    TelegramDeliveryClient,
    format_alert_message,
    format_digest_message,
    split_telegram_message,
)

__all__ = [
    "TelegramDeliveryClient",
    "format_alert_message",
    "format_digest_message",
    "split_telegram_message",
]
