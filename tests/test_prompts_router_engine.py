from types import SimpleNamespace

import pytest

from alphamacro.engine import AlphaMacroEngine
from alphamacro.exceptions import AgentExecutionError, InvalidQueryError
from alphamacro.models import AgentResult, RouteQuery, Source
from alphamacro.prompts import build_system_prompt
from alphamacro.router import IntentRouter

from .conftest import make_message as msg


# ---- prompts ---------------------------------------------------------
def test_prompt_contains_date_and_category():
    prompt = build_system_prompt("MACRO", "2026-01-01 10:00 WIB")
    assert "2026-01-01 10:00 WIB" in prompt
    assert "MODE RISET: MACRO" in prompt
    assert "$current_date" not in prompt


def test_prompt_unknown_category_falls_back():
    assert "Gabungkan" in build_system_prompt("UNKNOWN", "now")


# ---- router ----------------------------------------------------------
class FakeStructuredLLM:
    def __init__(self, result=None, error=None):
        self._result, self._error = result, error

    def invoke(self, _messages):
        if self._error:
            raise self._error
        return self._result


def test_router_success():
    expected = RouteQuery(category="MACRO", reasoning="cpi")
    assert IntentRouter(FakeStructuredLLM(expected)).classify("cpi?") == expected


@pytest.mark.parametrize(
    "llm", [FakeStructuredLLM(error=RuntimeError("boom")), FakeStructuredLLM({})]
)
def test_router_falls_back(llm):
    assert IntentRouter(llm).classify("x").category == "FULL_RESEARCH"


# ---- models ----------------------------------------------------------
def test_render_includes_sources_disclaimer_and_optional_header():
    result = AgentResult(
        "q", "MACRO", "r", "now", "isi", sources=[Source("A", "https://a.com")]
    )
    full = result.render_markdown()
    assert "# ALPHA INTELLIGENCE REPORT" in full
    assert "https://a.com" in full
    assert "bukan merupakan nasihat investasi" in full
    assert "ALPHA INTELLIGENCE REPORT" not in result.render_markdown(
        include_header=False
    )


# ---- engine ----------------------------------------------------------
class FakeRouter:
    def classify(self, _query):
        return RouteQuery(category="CRYPTO_MARKET", reasoning="harga")


class FakeAgent:
    def __init__(self, messages):
        self._messages = messages
        self.config = None

    def invoke(self, _payload, config=None):
        self.config = config
        return {"messages": self._messages}


def make_engine(settings, messages=None, agent_error=None):
    agent = FakeAgent(messages or [])

    def factory(_category, _now):
        if agent_error:
            raise agent_error
        return agent

    engine = AlphaMacroEngine(
        settings,
        llm=SimpleNamespace(),
        tools=[],
        router=FakeRouter(),
        agent_factory=factory,
    )
    return engine, agent


def test_engine_happy_path(settings):
    engine, agent = make_engine(
        settings,
        [
            msg("tool", "CRYPTO MARKET DATA", "get_crypto_market_data"),
            msg("ai", "BTC naik"),
        ],
    )
    result = engine.run("  harga btc  ")
    assert result.report == "BTC naik"
    assert result.category == "CRYPTO_MARKET"
    assert result.query == "harga btc"
    assert result.warnings == []
    assert agent.config == {"recursion_limit": settings.recursion_limit}


def test_engine_warns_when_no_tools(settings):
    engine, _ = make_engine(settings, [msg("ai", "tebakan")])
    assert engine.run("harga btc").warnings


def test_engine_empty_answer_raises(settings):
    engine, _ = make_engine(settings, [msg("ai", "")])
    with pytest.raises(AgentExecutionError):
        engine.run("harga btc")


def test_engine_wraps_agent_errors(settings):
    engine, _ = make_engine(settings, agent_error=RuntimeError("secret detail"))
    with pytest.raises(AgentExecutionError) as info:
        engine.run("harga btc")
    assert "secret detail" not in str(info.value)


def test_engine_validates_input(settings):
    engine, _ = make_engine(settings)
    with pytest.raises(InvalidQueryError):
        engine.run("   ")
