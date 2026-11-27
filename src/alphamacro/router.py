"""Intent router with retry and a safe fallback."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Sequence
from typing import Any

from alphamacro.constants import DEFAULT_CATEGORY
from alphamacro.models import RouteQuery
from alphamacro.prompts import ROUTER_PROMPT

logger = logging.getLogger(__name__)

_TRANSIENT_RE = re.compile(
    r"\b(429|500|502|503|504)\b"
    r"|UNAVAILABLE|DEADLINE_EXCEEDED|RESOURCE_EXHAUSTED|overloaded",
    re.IGNORECASE,
)

# (keywords, category name). Example rule; use a category that is valid in
# your RouteQuery model:
#   (("harga", "price", "berapa harga"), "MARKET_DATA"),
KeywordRule = tuple[tuple[str, ...], str]


def _is_transient(exc: BaseException) -> bool:
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, TimeoutError):
            return True
        if _TRANSIENT_RE.search(f"{type(cur).__name__}: {cur}"):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


class IntentRouter:
    """Classify a query into a research category; never raises.

    Order: LLM (with one retry on transient errors) -> keyword heuristic
    (optional) -> ``DEFAULT_CATEGORY``.
    """

    def __init__(
        self,
        structured_llm: Any,
        *,
        retries: int = 1,
        retry_delay: float = 1.0,
        keyword_rules: Sequence[KeywordRule] | None = None,
    ) -> None:
        self._llm = structured_llm
        self._retries = max(0, retries)
        self._retry_delay = retry_delay
        self._keyword_rules = tuple(keyword_rules or ())

    def classify(self, query: str) -> RouteQuery:
        started = time.perf_counter()
        last_exc: Exception | None = None

        for attempt in range(self._retries + 1):
            try:
                result = self._llm.invoke([("system", ROUTER_PROMPT), ("human", query)])
                if not isinstance(result, RouteQuery):
                    raise TypeError(
                        f"Router output has wrong type: {type(result).__name__}"
                    )
                logger.info(
                    "Router -> %s (%.2fs)",
                    result.category,
                    time.perf_counter() - started,
                )
                return result
            except Exception as exc:
                last_exc = exc
                if attempt < self._retries and _is_transient(exc):
                    logger.info(
                        "Router transient error (%s); retry %d/%d",
                        type(exc).__name__,
                        attempt + 1,
                        self._retries,
                    )
                    time.sleep(self._retry_delay)
                    continue
                break

        heuristic = self._keyword_route(query)
        if heuristic is not None:
            logger.warning(
                "Router failed (%s: %s); using keyword fallback -> %s",
                type(last_exc).__name__,
                last_exc,
                heuristic.category,
            )
            return heuristic

        logger.warning(
            "Router failed (%s: %s); falling back to %s",
            type(last_exc).__name__,
            last_exc,
            DEFAULT_CATEGORY,
        )
        return RouteQuery(
            category=DEFAULT_CATEGORY,  # type: ignore[arg-type]
            reasoning=(
                "Fallback otomatis: router tidak dapat mengklasifikasikan pertanyaan."
            ),
        )

    def _keyword_route(self, query: str) -> RouteQuery | None:
        lowered = query.lower()
        for keywords, category in self._keyword_rules:
            if any(kw.lower() in lowered for kw in keywords):
                try:
                    return RouteQuery(
                        category=category,  # type: ignore[arg-type]
                        reasoning=(
                            "Fallback heuristik kata kunci: router LLM tidak tersedia."
                        ),
                    )
                except Exception:  # invalid category -> fall through to the default
                    logger.debug("Invalid keyword rule category %r", category)
        return None
