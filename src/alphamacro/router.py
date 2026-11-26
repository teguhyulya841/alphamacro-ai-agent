"""Intent router with a safe fallback."""

from __future__ import annotations

import logging
import time
from typing import Any

from alphamacro.constants import DEFAULT_CATEGORY
from alphamacro.models import RouteQuery
from alphamacro.prompts import ROUTER_PROMPT

logger = logging.getLogger(__name__)


class IntentRouter:
    """Classify a query into a research category; never raises."""

    def __init__(self, structured_llm: Any) -> None:
        self._llm = structured_llm

    def classify(self, query: str) -> RouteQuery:
        started = time.perf_counter()
        try:
            result = self._llm.invoke([("system", ROUTER_PROMPT), ("human", query)])
            if not isinstance(result, RouteQuery):
                raise TypeError(
                    f"Router output has wrong type: {type(result).__name__}"
                )
            logger.info(
                "Router -> %s (%.2fs)", result.category, time.perf_counter() - started
            )
            return result
        except Exception as exc:
            logger.warning(
                "Router failed (%s: %s); falling back to %s",
                type(exc).__name__,
                exc,
                DEFAULT_CATEGORY,
            )
        return RouteQuery(
            category=DEFAULT_CATEGORY,  # type: ignore[arg-type]
            reasoning=(
                "Fallback otomatis: router tidak dapat mengklasifikasikan pertanyaan."
            ),
        )
