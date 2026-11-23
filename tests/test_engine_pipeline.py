"""Integration of guard, grounding, compliance and audit inside the engine (no network)."""

from types import SimpleNamespace

import pytest

from alphamacro.audit import AuditLog
from alphamacro.config import ApiSettings, Settings
from alphamacro.engine import AlphaMacroEngine
from alphamacro.exceptions import (
    AgentExecutionError,
    ConfigurationError,
    PolicyViolationError,
)
from alphamacro.models import RouteQuery

from .conftest import make_message as msg

MARKET_OUTPUT = "CRYPTO MARKET DATA: Bitcoin\nPrice USD: $98,512.34\n24H Change: -2.46%"


class Router:
    def classify(self, _q):
        return RouteQuery(category="CRYPTO_MARKET", reasoning="harga")


class Agent:
    def __init__(self, messages):
        self.messages = messages
        self.seen = None

    def invoke(self, payload, config=None):
        self.seen = payload
        return {"messages": self.messages}


def engine_with(settings, messages, audit=None):
    agent = Agent(messages)
    engine = AlphaMacroEngine(
        settings, llm=SimpleNamespace(), tools=[], router=Router(),
        agent_factory=lambda c, n: agent, audit=audit,
    )  # fmt: skip
    return engine, agent


def test_grounded_report_scores_high_and_has_no_flags(settings):
    engine, _ = engine_with(
        settings,
        [
            msg("tool", MARKET_OUTPUT, "get_crypto_market_data"),
            msg("ai", "BTC di $98,512.34, -2.46%."),
        ],
    )
    result = engine.run("harga btc")
    assert result.grounding.score == 1.0
    assert result.compliance_flags == [] and result.warnings == []
    assert "Verifikasi Numerik" in result.render_markdown()


def test_fabricated_numbers_trigger_warning_and_are_listed(settings):
    engine, _ = engine_with(
        settings,
        [
            msg("tool", MARKET_OUTPUT, "get_crypto_market_data"),
            msg("ai", "BTC $98,512.34 target $150,000 dan naik 12.5%."),
        ],
    )
    result = engine.run("harga btc")
    assert result.grounding.score < settings.grounding_min_score
    assert any("tidak dapat diverifikasi" in w for w in result.warnings)
    assert "$150,000" in result.grounding.ungrounded
    assert any("Target harga" in f for f in result.compliance_flags)


def test_secret_in_query_never_reaches_the_agent(settings):
    engine, agent = engine_with(settings, [msg("ai", "x")])
    with pytest.raises(PolicyViolationError):
        engine.run("key saya " + "AI" + "za" + "A" * 35)
    assert agent.seen is None


def test_pii_is_redacted_before_agent_and_flagged(settings):
    engine, agent = engine_with(
        settings,
        [msg("tool", MARKET_OUTPUT, "get_crypto_market_data"), msg("ai", "ok")],
    )
    result = engine.run("email saya a@b.com, harga btc?")
    assert "a@b.com" not in agent.seen["messages"][0]["content"]
    assert "a@b.com" not in result.query
    assert any("Data pribadi" in f for f in result.compliance_flags)


def test_audit_trail_records_ok_blocked_and_error(settings, tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl", salt="salt-value-123")
    engine, _ = engine_with(
        settings,
        [msg("tool", MARKET_OUTPUT, "get_crypto_market_data"), msg("ai", "ok")],
        audit=log,
    )
    engine.run("harga btc", user_id=42, channel="telegram", request_id="req-ok")
    with pytest.raises(PolicyViolationError):
        engine.run("key " + "AI" + "za" + "A" * 35, user_id=42)
    broken, _ = engine_with(settings, [msg("ai", "")], audit=log)
    with pytest.raises(AgentExecutionError):
        broken.run("harga btc")

    lines = [
        __import__("json").loads(x)
        for x in (tmp_path / "audit.jsonl").read_text().splitlines()
    ]
    assert [r["status"] for r in lines] == ["ok", "blocked", "error"]
    ok, blocked, _ = lines
    assert ok["request_id"] == "req-ok" and ok["channel"] == "telegram"
    assert ok["user"] == log.pseudonymize(42) and ok["user"] != "42"
    assert (
        "query" not in ok and len(ok["query_sha256"]) == 64
    )  # text not stored by default
    assert "query_sha256" not in blocked  # a blocked secret is never hashed or stored
    assert AuditLog.verify(tmp_path / "audit.jsonl").ok


def test_audit_can_store_redacted_query_text_when_enabled(settings, tmp_path):
    log = AuditLog(tmp_path / "a.jsonl", salt="salt-value-123", store_query_text=True)
    engine, _ = engine_with(settings, [msg("ai", "ok")], audit=log)
    engine.run("kontak a@b.com tentang btc")
    record = __import__("json").loads(
        (tmp_path / "a.jsonl").read_text().splitlines()[0]
    )
    assert "[EMAIL]" in record["query"] and "a@b.com" not in record["query"]


def test_audit_outage_does_not_break_request(settings, tmp_path, caplog):
    class BrokenAudit:
        store_query_text = False

        def pseudonymize(self, u):
            return "p"

        def record(self, event):
            raise OSError("disk full")

    engine, _ = engine_with(settings, [msg("ai", "ok")], audit=BrokenAudit())
    assert engine.run("harga btc").report == "ok"
    assert "AUDIT WRITE FAILED" in caplog.text


def test_technical_category_warns_when_indicator_tool_unused(settings):
    class TechRouter:
        def classify(self, _q):
            return RouteQuery(category="TECHNICAL", reasoning="rsi")

    engine = AlphaMacroEngine(
        settings, llm=SimpleNamespace(), tools=[], router=TechRouter(),
        agent_factory=lambda c, n: Agent(
            [msg("tool", "berita", "tavily_search"), msg("ai", "RSI tinggi")]
        ),
    )  # fmt: skip
    assert any("Indikator teknikal" in w for w in engine.run("rsi btc").warnings)


# ---- config ------------------------------------------------------------
def test_audit_requires_salt(clean_env):
    clean_env.setenv("GOOGLE_API_KEY", "g")
    clean_env.setenv("TAVILY_API_KEY", "t")
    clean_env.setenv("AUDIT_LOG_PATH", "x.jsonl")
    with pytest.raises(ConfigurationError, match="AUDIT_SALT"):
        Settings.from_env(load_env=False)
    clean_env.setenv("AUDIT_SALT", "s" * 16)
    s = Settings.from_env(load_env=False)
    assert s.audit_log_path == "x.jsonl" and ("s" * 16) in s.secrets()


@pytest.mark.parametrize("value", ["2", "-0.1", "abc"])
def test_grounding_min_score_validated(clean_env, value):
    clean_env.setenv("GOOGLE_API_KEY", "g")
    clean_env.setenv("TAVILY_API_KEY", "t")
    clean_env.setenv("GROUNDING_MIN_SCORE", value)
    with pytest.raises(ConfigurationError):
        Settings.from_env(load_env=False)


def test_api_settings_parsing(clean_env):
    digest = "a" * 64
    clean_env.setenv("API_KEY_HASHES", f"acme:{digest}, beta:{'b' * 64}")
    s = ApiSettings.from_env(load_env=False)
    assert set(s.api_key_hashes) == {"acme", "beta"}
    assert s.host == "127.0.0.1"
    assert digest not in repr(s)


@pytest.mark.parametrize("raw", ["acme", "acme:short", ":" + "a" * 64, " , "])
def test_api_settings_rejects_bad_hashes(clean_env, raw):
    clean_env.setenv("API_KEY_HASHES", raw)
    with pytest.raises(ConfigurationError):
        ApiSettings.from_env(load_env=False)


def test_api_settings_requires_keys(clean_env):
    with pytest.raises(ConfigurationError, match="API_KEY_HASHES"):
        ApiSettings.from_env(load_env=False)
