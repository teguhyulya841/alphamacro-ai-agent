"""Orchestration: guard -> router -> research agent -> verification -> audit."""

from __future__ import annotations

import asyncio
import inspect
import logging
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

logger = logging.getLogger(__name__)

AgentFactory = Callable[[str, str], Any]


class AlphaMacroEngine:
    """Async-first orchestration engine with a thread-safe sync bridge for CLI/bot.

    Pipeline per request:
        input guard (secrets blocked, PII redacted) -> intent router -> async tool agent
        -> provenance & numeric grounding + confidence + compliance -> audit record.
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

        self.llm = llm if llm is not None else self._default_llm()
        self.router = (
            router
            if router is not None
            else IntentRouter(self.llm.with_structured_output(RouteQuery))
        )
        self.market_client: AsyncCoinGeckoClient | None = None
        self.tools = list(tools) if tools is not None else self._default_tools()
        self._agent_factory: AgentFactory = agent_factory or self._build_agent
        self.audit = audit if audit is not None else self._default_audit()
        self._loop_thread = LoopThread(name="alphamacro-engine-loop")

    # -- default wiring ---------------------------------------------------

    def _default_llm(self) -> Any:
        return ChatGoogleGenerativeAI(
            model=self.settings.gemini_model,
            google_api_key=self.settings.google_api_key.get_secret_value(),
            timeout=self.settings.llm_timeout,
            max_retries=self.settings.llm_max_retries,
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
        return [
            build_async_market_data_tool(self.market_client),
            build_async_technical_tool(self.market_client),
            TavilySearch(
                max_results=5,
                topic="finance",
                search_depth="advanced",
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

    def _build_agent(self, category: str, current_date: str) -> Any:
        return create_agent(
            model=self.llm,
            tools=self.tools,
            system_prompt=build_system_prompt(category, current_date),
        )

    # -- lifecycle --------------------------------------------------------

    async def aclose(self) -> None:
        """Close owned async HTTP clients and background cache tasks."""
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
        try:
            agent = self._agent_factory(route.category, now)
            response = await self._invoke_agent(agent, query)
        except Exception as exc:
            logger.exception("Agent execution failed request_id=%s", request_id)
            raise AgentExecutionError(
                f"Agent gagal dijalankan ({type(exc).__name__}). "
                "Periksa log untuk detail."
            ) from exc

        messages = response.get("messages", []) if isinstance(response, dict) else []
        report = extract_final_answer(messages)
        if not report:
            raise AgentExecutionError("Agent tidak menghasilkan jawaban.")

        tools_used, sources = inspect_tool_activity(messages)
        observations = collect_observations(messages, now=now_dt)
        warnings = [
            *build_quality_warnings(route.category, tools_used),
            *build_staleness_warnings(observations),
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
            confidence=confidence,
        )
