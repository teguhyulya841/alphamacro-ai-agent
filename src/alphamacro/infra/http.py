"""Shared async HTTP client factory and bounded retry helper with jitter."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import httpx

from alphamacro import __version__

logger = logging.getLogger(__name__)

_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


def create_async_client(
    *,
    base_url: str = "",
    timeout_s: float = 15.0,
    headers: dict[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """Create a configured ``httpx.AsyncClient`` with standard User-Agent and limits."""
    merged_headers = {
        "accept": "application/json",
        "user-agent": f"alphamacro-crypto/{__version__}",
    }
    if headers:
        merged_headers.update(headers)
    timeout = httpx.Timeout(timeout_s, connect=min(10.0, timeout_s))
    limits = httpx.Limits(max_connections=32, max_keepalive_connections=16)
    return httpx.AsyncClient(
        base_url=base_url,
        headers=merged_headers,
        timeout=timeout,
        limits=limits,
        transport=transport,
    )


def _retry_delay(
    response: httpx.Response | None, attempt: int, base_backoff: float
) -> float:
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(30.0, max(0.1, float(retry_after)))
            except ValueError:
                pass
    exp = base_backoff * (2**attempt)
    jitter = random.uniform(0.0, base_backoff)  # noqa: S311
    return min(30.0, exp + jitter)


async def get_json_with_retry(
    client: httpx.AsyncClient,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    max_retries: int = 3,
    base_backoff_s: float = 0.5,
) -> Any:
    """Perform an async GET and decode JSON with bounded exponential backoff."""
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            response = await client.get(path, params=params)
            if response.status_code in _RETRYABLE_STATUSES and attempt < max_retries:
                delay = _retry_delay(response, attempt, base_backoff_s)
                logger.warning(
                    "Transient HTTP %s on %s (attempt %d/%d); retrying in %.2fs",
                    response.status_code,
                    path,
                    attempt + 1,
                    max_retries + 1,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            response.raise_for_status()
            return response.json()
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            if attempt >= max_retries:
                raise
            delay = _retry_delay(None, attempt, base_backoff_s)
            logger.warning(
                "Transport error %s on %s (attempt %d/%d); retrying in %.2fs",
                type(exc).__name__,
                path,
                attempt + 1,
                max_retries + 1,
                delay,
            )
            await asyncio.sleep(delay)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Unreachable retry state")
