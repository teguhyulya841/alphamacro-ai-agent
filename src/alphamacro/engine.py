"""Orchestration: guard -> router -> research agent -> verification -> audit."""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import re
import time
import uuid
from collections.abc import Callable, Sequence
from typing import Any

from langchain.agents import create_agent
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_tavily import TavilySearch

from alphamacro import __version__
from alphamacro.audit import AuditLog, sha256_text
from alphamacro.clock import SystemClock
from alphamacro.compliance import scan_input, scan_output
from alphamacro.config import Settings, StateSettings
from alphamacro.constants import WIB
from alphamacro.exceptions import (
    AgentExecutionError,
    InvalidQueryError,
    PolicyViolationError,
)
from alphamacro.grounding import (
    assess_confidence,
    build_staleness_warnings,
    verify_numeric_grounding,
)
from alphamacro.infra.caching import DEFAULT_POLICIES, CachingFeed
from alphamacro.infra.state import StateBundle, build_state
from alphamacro.infra.sync_bridge import LoopThread
from alphamacro.models import (
    AgentResult,
    CacheClass,
    FreshnessPolicy,
    RouteQuery,
    StateBackend,
)
from alphamacro.parsing import (
    build_quality_warnings,
    collect_observations,
    collect_tool_outputs,
    extract_final_answer,
    inspect_tool_activity,
    sanitize_query,
)
from alphamacro.ports import Clock
from alphamacro.prompts import build_system_prompt
from alphamacro.router import IntentRouter
from alphamacro.tools import (
    AsyncCoinGeckoClient,
    build_async_market_data_tool,
    build_async_technical_tool,
)
from alphamacro.tools.venues import MultiVenueValidator

logger = logging.getLogger(__name__)

AgentFactory = Callable[[str, str], Any]

# Transient errors (overload, timeout, rate limit) that are safe to retry.
_TRANSIENT_RE = re.compile(
    r"\b(429|500|502|503|504)\b"
    r"|UNAVAILABLE|DEADLINE_EXCEEDED|RESOURCE_EXHAUSTED|overloaded",
    re.IGNORECASE,
)


def _is_transient(exc: BaseException) -> bool:
    """Return True if the error (or any cause in its chain) is retryable."""
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


class AlphaMacroEngine:
    """Async-first orchestration engine with a thread-safe sync bridge for CLI/bot.

    Pipeline per request:
        input guard (secrets blocked, PII redacted) -> intent router -> async tool agent
        (retry + model fallback) -> provenance & multi-venue validation + grounding
        + confidence -> audit.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        llm: Any | None = None,
        tools: Sequence[Any] | None = None,
        router: Any | None = None,
        agent_factory: AgentFactory | None = None,
        audit: AuditLog | None = None,
        state_bundle: StateBundle | None = None,
        state_settings: StateSettings | None = None,
        clock: Clock | None = None,
        venue_validator: MultiVenueValidator | None = None,
    ) -> None:
        self.settings = settings
        self.clock: Clock = clock if clock is not None else SystemClock()
        if state_bundle is not None:
            self.state = state_bundle
        elif state_settings is not None:
            self.state = build_state(state_settings, clock=self.clock)
        else:
            self.state = build_state(
                StateSettings(backend=StateBackend.MEMORY), clock=self.clock
            )

        llm_injected = llm is not None

        # Agent LLM: long timeout, since multi-tool research takes time.
        self.llm = (
            llm
            if llm is not None
            else self._make_llm(
                settings.gemini_model,
                timeout=settings.agent_llm_timeout,
                max_retries=settings.llm_max_retries,
            )
        )

        # Router: must be fast and cheap, so it gets a short, separate timeout.
        if router is not None:
            self.router = router
        else:
            router_llm = (
                self.llm
                if llm_injected
                else self._make_llm(
                    settings.gemini_model,
                    timeout=settings.router_llm_timeout,
                    max_retries=1,
                )
            )
            self.router = IntentRouter(router_llm.with_structured_output(RouteQuery))

        # Fallback model for the last attempt if the primary model keeps failing.
        self.fallback_llm: Any | None = (
            self._make_llm(
                settings.gemini_fallback_model,
                timeout=settings.agent_llm_timeout,
                max_retries=1,
            )
            if settings.gemini_fallback_model and not llm_injected
            else None
        )

        self.market_client: AsyncCoinGeckoClient | None = None
        self.venue_validator: MultiVenueValidator | None = venue_validator
        self.tools = list(tools) if tools is not None else self._default_tools()
        self._agent_factory: AgentFactory = agent_factory or self._build_agent
        self.audit = audit if audit is not None else self._default_audit()
        self._loop_thread = LoopThread(name="alphamacro-engine-loop")

    # -- default wiring ---------------------------------------------------

    def _make_llm(self, model: str, *, timeout: float, max_retries: int) -> Any:
        return ChatGoogleGenerativeAI(
            model=model,
            google_api_key=self.settings.google_api_key.get_secret_value(),
            timeout=timeout,
            max_retries=max_retries,
        )

    def _default_tools(self) -> list[Any]:
        coingecko_key = (
            self.settings.coingecko_api_key.get_secret_value()
            if self.settings.coingecko_api_key is not None
            else None
        )
        caching_feed: CachingFeed | None = None
        if self.settings.cache_ttl > 0:
            policies = dict(DEFAULT_POLICIES)
            if self.settings.cache_ttl != 60:
                policies[CacheClass.MARKETS] = FreshnessPolicy(
                    fresh_ttl_s=self.settings.cache_ttl,
                    stale_ttl_s=max(self.settings.cache_ttl * 15, 900),
                )
            caching_feed = CachingFeed(
                self.state.cache,
                self.state.locks,
                self.clock,
                policies=policies,
            )

        self.market_client = AsyncCoinGeckoClient(
            caching_feed=caching_feed,
            timeout=self.settings.http_timeout,
            cache_ttl=self.settings.cache_ttl,
            api_key=coingecko_key,
            clock=self.clock,
        )
        if self.venue_validator is None:
            self.venue_validator = MultiVenueValidator(
                caching_feed=caching_feed,
                clock=self.clock,
            )
        return [
            build_async_market_data_tool(
                self.market_client, validator=self.venue_validator
            ),
            build_async_technical_tool(self.market_client),
            TavilySearch(
                max_results=self.settings.tavily_max_results,
                topic="finance",
                search_depth=self.settings.tavily_search_depth,
                tavily_api_key=self.settings.tavily_api_key.get_secret_value(),
            ),
        ]

    def _default_audit(self) -> AuditLog | None:
        if not self.settings.audit_log_path:
            return None
        salt = (
            self.settings.audit_salt.get_secret_value()
            if self.settings.audit_salt is not None
            else ""
        )
        return AuditLog(
            self.settings.audit_log_path,
            salt=salt,
            store_query_text=self.settings.audit_store_query_text,
        )

    def _build_agent(
        self, category: str, current_date: str, llm: Any | None = None
    ) -> Any:
        return create_agent(
            model=llm if llm is not None else self.llm,
            tools=self.tools,
            system_prompt=build_system_prompt(category, current_date),
        )

    # -- lifecycle --------------------------------------------------------

    async def aclose(self) -> None:
        """Close owned async HTTP clients and background cache tasks."""
        if self.venue_validator is not None:
            await self.venue_validator.aclose()
        if self.market_client is not None:
            await self.market_client.aclose()

    def close(self) -> None:
        """Shut down owned async resources and the background event loop thread."""
        try:
            self._loop_thread.run(self.aclose(), timeout=5.0)
        except Exception:
            logger.debug(
                "Ignoring error during engine async resource close", exc_info=True
            )
        finally:
            self._loop_thread.close()

    # -- public API -------------------------------------------------------

    def run(
        self,
        query: str,
        *,
        user_id: int | str | None = None,
        channel: str = "cli",
        request_id: str | None = None,
    ) -> AgentResult:
        """Synchronous entry point delegating to the background asyncio loop."""
        return self._loop_thread.run(
            self.arun(
                query,
                user_id=user_id,
                channel=channel,
                request_id=request_id,
            )
        )

    async def arun(
        self,
        query: str,
        *,
        user_id: int | str | None = None,
        channel: str = "cli",
        request_id: str | None = None,
    ) -> AgentResult:
        """Run the full research pipeline asynchronously."""
        request_id = request_id or uuid.uuid4().hex[:12]
        started = time.perf_counter()
        event: dict[str, Any] = {
            "request_id": request_id,
            "channel": channel,
            "model": self.settings.gemini_model,
            "version": __version__,
            "status": "error",
        }
        if user_id is not None and self.audit is not None:
            event["user"] = self.audit.pseudonymize(user_id)
        try:
            result = await self._arun(query, request_id, started, event)
            event["status"] = "ok"
            return result
        except PolicyViolationError as exc:
            event.update(status="blocked", error_type=type(exc).__name__)
            raise
        except InvalidQueryError as exc:
            event.update(status="rejected", error_type=type(exc).__name__)
            raise
        except Exception as exc:
            event["error_type"] = type(exc).__name__
            raise
        finally:
            event["latency_seconds"] = round(time.perf_counter() - started, 2)
            await self._awrite_audit(event)

    async def _awrite_audit(self, event: dict[str, Any]) -> None:
        if self.audit is None:
            return
        try:
            await asyncio.to_thread(self.audit.record, event)
        except Exception:  # audit outage must be loud, not crash service
            logger.exception(
                "AUDIT WRITE FAILED request_id=%s", event.get("request_id")
            )

    async def _classify_route(self, query: str) -> RouteQuery:
        aclassify = getattr(self.router, "aclassify", None)
        if inspect.iscoroutinefunction(aclassify):
            return await aclassify(query)
        return await asyncio.to_thread(self.router.classify, query)

    async def _invoke_agent(self, agent: Any, query: str) -> Any:
        payload = {"messages": [{"role": "user", "content": query}]}
        config = {"recursion_limit": self.settings.recursion_limit}
        ainvoke = getattr(agent, "ainvoke", None)
        if inspect.iscoroutinefunction(ainvoke):
            return await ainvoke(payload, config=config)
        return await asyncio.to_thread(agent.invoke, payload, config=config)

    async def _run_agent_resilient(
        self,
        category: str,
        now: str,
        query: str,
        request_id: str,
        event: dict[str, Any],
    ) -> Any:
        """Run the agent with retry (backoff + jitter) and model fallback.

        - Only transient errors (503/504/429/timeout) are retried.
        - The last attempt uses ``fallback_llm`` when configured.
        - All attempts together are bounded by ``agent_overall_timeout``.
        """
        max_attempts = max(1, self.settings.agent_max_attempts)
        overall_timeout = self.settings.agent_overall_timeout
        deadline = time.monotonic() + overall_timeout
        last_exc: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            use_fallback = (
                attempt == max_attempts
                and attempt > 1
                and self.fallback_llm is not None
            )
            try:
                if use_fallback:
                    logger.warning(
                        "Switching to fallback model request_id=%s",
                        request_id,
                    )
                    event["fallback_used"] = True
                    agent = self._build_agent(category, now, llm=self.fallback_llm)
                else:
                    agent = self._agent_factory(category, now)
                return await asyncio.wait_for(
                    self._invoke_agent(agent, query), timeout=remaining
                )
            except Exception as exc:
                last_exc = exc
                event["agent_attempts"] = attempt
                if not _is_transient(exc) or attempt == max_attempts:
                    break
                jitter = random.uniform(0, 1)  # noqa: S311
                delay = min(2**attempt, 20) + jitter
                if time.monotonic() + delay >= deadline:
                    break
                logger.warning(
                    "Transient agent error (attempt %d/%d) request_id=%s: %s; "
                    "retry in %.1fs",
                    attempt,
                    max_attempts,
                    request_id,
                    type(exc).__name__,
                    delay,
                )
                await asyncio.sleep(delay)

        if last_exc is None:  # overall deadline expired before the first attempt
            last_exc = TimeoutError("agent overall timeout")

        logger.error(
            "Agent execution failed request_id=%s",
            request_id,
            exc_info=last_exc,
        )
        if _is_transient(last_exc):
            raise AgentExecutionError(
                "Layanan model (Gemini) sedang sibuk atau timeout. "
                "Coba lagi dalam beberapa menit."
            ) from last_exc
        raise AgentExecutionError(
            f"Agent gagal dijalankan ({type(last_exc).__name__}). "
            "Periksa log untuk detail."
        ) from last_exc

    async def _arun(
        self, query: str, request_id: str, started: float, event: dict[str, Any]
    ) -> AgentResult:
        query = sanitize_query(query, self.settings.max_query_chars)

        scan = scan_input(query)
        query = scan.redacted_query
        event["query_sha256"] = sha256_text(query)
        if self.audit is not None and self.audit.store_query_text:
            event["query"] = query

        route = await self._classify_route(query)
        event["category"] = route.category
        now_dt = self.clock.now()
        now = now_dt.astimezone(WIB).strftime("%Y-%m-%d %H:%M WIB")

        logger.info("Running agent (mode=%s) request_id=%s", route.category, request_id)
        response = await self._run_agent_resilient(
            route.category, now, query, request_id, event
        )

        messages = response.get("messages", []) if isinstance(response, dict) else []
        report = extract_final_answer(messages)
        if not report:
            raise AgentExecutionError("Agent tidak menghasilkan jawaban.")

        tools_used, sources = inspect_tool_activity(messages)
        observations = collect_observations(messages, now=now_dt)
        cross_validations = [
            obs.cross_validation
            for obs in observations
            if obs.cross_validation is not None
        ]
        cv_warnings = [w for cv in cross_validations for w in cv.warnings]
        warnings = [
            *build_quality_warnings(route.category, tools_used),
            *build_staleness_warnings(observations),
            *cv_warnings,
        ]

        grounding = verify_numeric_grounding(
            report,
            collect_tool_outputs(messages),
            observations=observations,
        )
        if (
            grounding.score is not None
            and grounding.score < self.settings.grounding_min_score
        ):
            warnings.append(
                f"Hanya {grounding.verified} dari {grounding.total} angka dalam "
                f"laporan yang cocok dengan data tool (skor {grounding.score:.0%}); "
                "sisanya tidak dapat diverifikasi dan sebaiknya dicek ulang."
            )
        confidence = assess_confidence(
            grounding=grounding,
            observations=observations,
            cross_validations=cross_validations,
            grounding_min_score=self.settings.grounding_min_score,
        )
        compliance_flags = [*scan.flags, *scan_output(report)]

        for warning in warnings:
            logger.warning(warning)
        latency = time.perf_counter() - started
        logger.info(
            "Completed in %.2fs | tools=%s | sources=%d | grounding=%s | confidence=%s",
            latency,
            tools_used,
            len(sources),
            "n/a" if grounding.score is None else f"{grounding.score:.0%}",
            confidence.level.value,
        )

        event.update(
            tools=tools_used,
            source_urls=[s.url for s in sources[:10]],
            grounding_score=grounding.score,
            grounding_total=grounding.total,
            confidence_level=confidence.level.value,
            confidence_score=confidence.score,
            stale_observations=confidence.stale_observations,
            divergent_venues=sum(len(cv.divergent_venues) for cv in cross_validations),
            compliance_flags=len(compliance_flags),
            warnings=len(warnings),
        )

        return AgentResult(
            query=query,
            category=route.category,
            routing_reason=route.reasoning,
            analysis_time=now,
            report=report,
            tools_used=tools_used,
            sources=sources[:10],
            warnings=warnings,
            latency_seconds=round(latency, 2),
            request_id=request_id,
            model=self.settings.gemini_model,
            grounding=grounding,
            compliance_flags=compliance_flags,
            observations=observations,
            cross_validations=cross_validations,
            confidence=confidence,
        )
