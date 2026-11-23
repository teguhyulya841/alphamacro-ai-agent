"""Input sanitising and agent-trace parsing helpers (pure functions, easy to test)."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from pydantic import AwareDatetime

from alphamacro.constants import MARKET_TOOL_NAME, SEARCH_TOOL_NAME, TECHNICAL_TOOL_NAME
from alphamacro.exceptions import InvalidQueryError
from alphamacro.models import Freshness, Observation, Source

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_query(query: str, max_chars: int) -> str:
    """Validate and clean user input."""
    if not isinstance(query, str) or not query.strip():
        raise InvalidQueryError("Pertanyaan tidak boleh kosong.")
    cleaned = _CONTROL_CHARS_RE.sub("", query)
    cleaned = re.sub(r"[ \t]+", " ", cleaned).strip()
    if len(cleaned) > max_chars:
        raise InvalidQueryError(
            f"Pertanyaan terlalu panjang (maksimal {max_chars} karakter)."
        )
    return cleaned


def content_to_text(content: Any) -> str:
    """Normalise LangChain message content (str or list of blocks) to text."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(p for p in parts if p).strip()
    return "" if content is None else str(content).strip()


def _parse_tavily_results(content: Any) -> list[dict[str, Any]]:
    payload = content
    if isinstance(content, str):
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return []
    if isinstance(payload, dict):
        results = payload.get("results", [])
        return [r for r in results if isinstance(r, dict)]
    return []


def inspect_tool_activity(messages: Iterable[Any]) -> tuple[list[str], list[Source]]:
    """Return the tools actually called and the sources they actually returned."""
    tools_used: list[str] = []
    sources: list[Source] = []
    seen_urls: set[str] = set()

    def add_source(title: str, url: str) -> None:
        if url and url not in seen_urls:
            seen_urls.add(url)
            sources.append(Source(title=title or url, url=url))

    for message in messages:
        if getattr(message, "type", None) != "tool":
            continue
        name = getattr(message, "name", None) or "unknown"
        tools_used.append(name)

        if name == SEARCH_TOOL_NAME:
            for item in _parse_tavily_results(getattr(message, "content", None)):
                url = str(item.get("url", "")).strip()
                if url.startswith(("http://", "https://")):
                    add_source(str(item.get("title", "")).strip(), url)
        elif name in (MARKET_TOOL_NAME, TECHNICAL_TOOL_NAME) and not str(
            getattr(message, "content", "")
        ).startswith("TOOL ERROR"):
            label = (
                "CoinGecko API (market data)"
                if name == MARKET_TOOL_NAME
                else "CoinGecko API (price history, indicators computed locally)"
            )
            add_source(label, "https://www.coingecko.com")

    return tools_used, sources


def collect_observations(
    messages: Iterable[Any],
    *,
    now: AwareDatetime | None = None,
) -> list[Observation]:
    """Extract structured Observation provenance artifacts from tool messages."""
    ts = now if now is not None else datetime.now(UTC)
    observations: list[Observation] = []

    for message in messages:
        if getattr(message, "type", None) != "tool":
            continue
        text = content_to_text(getattr(message, "content", ""))
        if text.startswith("TOOL ERROR"):
            continue

        artifact = getattr(message, "artifact", None)
        if isinstance(artifact, Observation):
            observations.append(artifact)
            continue

        name = getattr(message, "name", None) or "unknown"
        if name == SEARCH_TOOL_NAME:
            results = _parse_tavily_results(getattr(message, "content", None))
            urls = [
                str(r.get("url", "")).strip()
                for r in results
                if str(r.get("url", "")).strip().startswith(("http://", "https://"))
            ]
            observations.append(
                Observation(
                    source="tavily",
                    endpoint="search",
                    entity="web",
                    observed_at=ts,
                    fetched_at=ts,
                    freshness=Freshness.FRESH,
                    age_s=0.0,
                    metrics={"result_count": len(results)},
                    urls=urls[:10],
                )
            )
        elif name in (MARKET_TOOL_NAME, TECHNICAL_TOOL_NAME) and text:
            endpoint = (
                "/coins/markets"
                if name == MARKET_TOOL_NAME
                else "/coins/technical"
            )
            observations.append(
                Observation(
                    source="coingecko",
                    endpoint=endpoint,
                    entity="crypto",
                    observed_at=ts,
                    fetched_at=ts,
                    freshness=Freshness.FRESH,
                    age_s=0.0,
                    urls=["https://www.coingecko.com"],
                )
            )

    return observations


def collect_tool_outputs(messages: Iterable[Any]) -> list[str]:
    """Raw text of every successful tool result (ground truth for numeric verification)."""
    outputs: list[str] = []
    for message in messages:
        if getattr(message, "type", None) != "tool":
            continue
        text = content_to_text(getattr(message, "content", ""))
        if text and not text.startswith("TOOL ERROR"):
            outputs.append(text)
    return outputs


def extract_final_answer(messages: list[Any]) -> str:
    """Return the last non-empty AI message."""
    for message in reversed(messages):
        if getattr(message, "type", None) == "ai":
            text = content_to_text(getattr(message, "content", ""))
            if text:
                return text
    return ""


def build_quality_warnings(category: str, tools_used: list[str]) -> list[str]:
    """Flag reports that were not backed by the expected tool calls."""
    warnings: list[str] = []
    if not tools_used:
        warnings.append(
            "Tidak ada tool yang dipanggil; isi laporan berasal dari pengetahuan model "
            "dan tidak terverifikasi dengan data terkini."
        )
    elif (
        category in ("CRYPTO_MARKET", "FULL_RESEARCH")
        and MARKET_TOOL_NAME not in tools_used
    ):
        warnings.append(
            "Data harga CoinGecko tidak diambil pada sesi ini; angka harga (jika ada) "
            "tidak terverifikasi."
        )
    if (
        category == "TECHNICAL"
        and TECHNICAL_TOOL_NAME not in tools_used
        and tools_used
    ):
        warnings.append(
            "Indikator teknikal tidak dihitung dari data pasar pada sesi ini; "
            "angka indikator (jika ada) tidak terverifikasi."
        )
    return warnings
