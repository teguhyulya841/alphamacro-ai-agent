import threading

import pytest
from fastapi.testclient import TestClient

from alphamacro.api.app import create_app
from alphamacro.api.auth import generate_key, hash_key, keygen_main, verify_key
from alphamacro.config import ApiSettings
from alphamacro.exceptions import (
    AgentExecutionError,
    CoinNotFoundError,
    InvalidQueryError,
    MarketDataError,
    PolicyViolationError,
)
from alphamacro.models import AgentResult, GroundingReport, Source

KEY, KEY_HASH = generate_key()


class FakeEngine:
    def __init__(self, error=None):
        self.error = error
        self.calls = []
        self.market_client = None

    def run(self, query, **kwargs):
        self.calls.append((query, kwargs))
        if self.error:
            raise self.error
        return AgentResult(
            query, "MACRO", "r", "now", "isi laporan",
            tools_used=["tavily_search"],
            sources=[Source("A", "https://a.com")],
            request_id=kwargs.get("request_id", ""),
            model="m",
            grounding=GroundingReport(total=2, verified=1, score=0.5,
            ungrounded=["9.9%"]
            ),
            compliance_flags=["flag"],
        )  # fmt: skip


def make_client(engine=None, rate=100, market_client=None, concurrent=4):
    settings = ApiSettings(
        api_key_hashes={"acme": KEY_HASH},
        rate_limit_per_minute=rate,
        max_concurrent_requests=concurrent,
    )
    engine = engine or FakeEngine()
    app = create_app(engine, settings, market_client=market_client)
    return TestClient(app, raise_server_exceptions=False), engine


HEADERS = {"X-API-Key": KEY}


def test_health_is_public():
    client, _ = make_client()
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json()["status"] == "ready"


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}, {"X-API-Key": ""}])
def test_auth_required(headers):
    client, engine = make_client()
    response = client.post("/v1/analyze", json={"query": "halo"}, headers=headers)
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "unauthorized"
    assert engine.calls == []


def test_analyze_success_schema_and_identity():
    client, engine = make_client()
    response = client.post("/v1/analyze", json={"query": "cpi?"}, headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["report"] == "isi laporan"
    assert body["grounding"] == {
        "total": 2,
        "verified": 1,
        "score": 0.5,
        "ungrounded": ["9.9%"],
    }
    assert body["sources"] == [{"title": "A", "url": "https://a.com"}]
    assert body["compliance_flags"] == ["flag"]
    assert "bukan merupakan nasihat investasi" in body["disclaimer"]
    _query, kwargs = engine.calls[0]
    assert kwargs["channel"] == "api" and kwargs["user_id"] == "acme"
    assert (
        response.headers["X-Request-ID"] == body["request_id"] == kwargs["request_id"]
    )
    assert response.headers["Cache-Control"] == "no-store"


def test_client_supplied_request_id_is_kept_only_if_safe():
    client, _ = make_client()
    ok = client.post(
        "/v1/analyze",
        json={"query": "x"},
        headers={**HEADERS, "X-Request-ID": "trace-12345678"},
    )
    assert ok.headers["X-Request-ID"] == "trace-12345678"
    bad = client.post(
        "/v1/analyze",
        json={"query": "x"},
        headers={**HEADERS, "X-Request-ID": "bad id\n<x>"},
    )
    assert bad.headers["X-Request-ID"] != "bad id\n<x>"


def test_validation_rejects_empty_query():
    client, _ = make_client()
    assert (
        client.post("/v1/analyze", json={"query": ""}, headers=HEADERS).status_code
        == 422
    )


def test_rate_limit_returns_429_with_retry_after():
    client, _ = make_client(rate=1)
    assert (
        client.post("/v1/analyze", json={"query": "a"}, headers=HEADERS).status_code
        == 200
    )
    response = client.post("/v1/analyze", json={"query": "b"}, headers=HEADERS)
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) >= 1


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (PolicyViolationError("rahasia terdeteksi"), 400, "policy_violation"),
        (InvalidQueryError("kosong"), 400, "invalid_query"),
        (
            AgentExecutionError("RuntimeError: api_key=SECRET123"),
            502,
            "analysis_failed",
        ),
    ],
)
def test_engine_errors_are_mapped_without_leaks(error, status, code):
    client, _ = make_client(FakeEngine(error=error))
    response = client.post("/v1/analyze", json={"query": "halo"}, headers=HEADERS)
    assert response.status_code == status
    detail = response.json()["detail"]
    assert detail["code"] == code and detail["request_id"]
    assert "SECRET123" not in response.text


def test_unexpected_exception_returns_generic_500():
    client, _ = make_client(FakeEngine(error=ZeroDivisionError("secret internals")))
    response = client.post("/v1/analyze", json={"query": "halo"}, headers=HEADERS)
    assert response.status_code == 500
    assert "secret internals" not in response.text


def test_concurrency_cap_returns_503():
    client, _ = make_client(concurrent=1)
    # Hold the only slot by running inside a blocked engine call.


    gate, entered = threading.Event(), threading.Event()

    class Blocking(FakeEngine):
        def run(self, query, **kwargs):
            entered.set()
            gate.wait(5)
            return super().run(query, **kwargs)

    client, _ = make_client(Blocking(), concurrent=1)
    results = []
    worker = threading.Thread(
        target=lambda: results.append(
            client.post("/v1/analyze", json={"query": "a"}, headers=HEADERS).status_code
        )
    )
    worker.start()
    assert entered.wait(5)
    second = client.post("/v1/analyze", json={"query": "b"}, headers=HEADERS)
    gate.set()
    worker.join(5)
    assert second.status_code == 503 and second.headers["Retry-After"] == "5"
    assert results == [200]


# ---- indicators endpoint -----------------------------------------------
class FakeMarket:
    def __init__(self, error=None):
        self.error = error

    def get_daily_closes(self, coin_id, days=365):
        if self.error:
            raise self.error
        return [100 + i * 0.3 for i in range(260)]

    def get_ohlc(self, coin_id, days=30):
        return [[i, 100, 102, 98, 100 + (i % 5)] for i in range(180)]


def test_indicators_endpoint_requires_auth_and_returns_snapshot():
    client, _ = make_client(market_client=FakeMarket())
    assert client.get("/v1/indicators/bitcoin").status_code == 401
    response = client.get("/v1/indicators/bitcoin?timeframe=daily", headers=HEADERS)
    assert response.status_code == 200
    snap = response.json()["indicators"]
    assert snap["coin_id"] == "bitcoin" and snap["rsi14"] is not None


def test_indicators_4h_and_validation():
    client, _ = make_client(market_client=FakeMarket())
    assert (
        client.get("/v1/indicators/bitcoin?timeframe=4h", headers=HEADERS).status_code
        == 200
    )
    assert (
        client.get("/v1/indicators/bitcoin?timeframe=1w", headers=HEADERS).status_code
        == 422
    )
    assert client.get("/v1/indicators/BAD..ID", headers=HEADERS).status_code in (
        404,
        422,
    )


@pytest.mark.parametrize(
    ("error", "status"),
    [(CoinNotFoundError("x"), 404), (MarketDataError("down"), 502)],
)
def test_indicators_error_mapping(error, status):
    client, _ = make_client(market_client=FakeMarket(error=error))
    assert client.get("/v1/indicators/bitcoin", headers=HEADERS).status_code == status


def test_indicators_unavailable_without_market_client():
    client, _ = make_client()
    assert client.get("/v1/indicators/bitcoin", headers=HEADERS).status_code == 503


# ---- auth helpers ------------------------------------------------------
def test_key_helpers():
    key, digest = generate_key()
    assert key.startswith("amc_") and digest == hash_key(key) and len(digest) == 64
    hashes = {"a": digest, "b": hash_key("other")}
    assert verify_key(key, hashes) == "a"
    assert verify_key("nope", hashes) is None
    assert verify_key(None, hashes) is None


def test_keygen_cli_prints_hash_not_stored_anywhere(capsys):
    assert keygen_main(["acme"]) == 0
    out = capsys.readouterr().out
    assert "API_KEY_HASHES=acme:" in out
