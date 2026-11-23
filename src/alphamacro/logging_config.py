"""Logging setup with secret redaction and optional JSON output."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterable

REDACTED = "***REDACTED***"
_NOISY_LOGGERS = ("httpx", "httpcore", "urllib3", "google", "grpc", "TeleBot")


def _redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, REDACTED)
    return text


class RedactingFormatter(logging.Formatter):
    """Text formatter that masks secrets in the final output (including tracebacks)."""

    def __init__(self, secrets: list[str], **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._secrets = secrets

    def format(self, record: logging.LogRecord) -> str:
        return _redact(super().format(record), self._secrets)


class JsonFormatter(logging.Formatter):
    """One JSON object per line; friendly to log aggregators in containers."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = secrets

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return _redact(json.dumps(payload, ensure_ascii=False), self._secrets)


def configure_logging(
    level: str = "INFO",
    secrets: Iterable[str] = (),
    json_logs: bool = False,
) -> None:
    """Configure root logging to stderr (stdout is reserved for program output)."""
    secret_list = [s for s in secrets if s and len(s) >= 8]
    handler = logging.StreamHandler(sys.stderr)
    if json_logs:
        handler.setFormatter(JsonFormatter(secret_list))
    else:
        handler.setFormatter(
            RedactingFormatter(
                secret_list,
                fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
