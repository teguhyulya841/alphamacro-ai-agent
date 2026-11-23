"""Contract tests for RateLimiter across memory and SQLite backends."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from alphamacro.clock import FrozenClock
from alphamacro.config import StateSettings
from alphamacro.infra.state import StateBundle, build_state
from alphamacro.models import RateRule, StateBackend


@pytest.fixture(params=[StateBackend.MEMORY, StateBackend.SQLITE])
def limiter_bundle(
    request: pytest.FixtureRequest, tmp_path: Path
) -> tuple[StateBundle, FrozenClock]:
    clock = FrozenClock(datetime(2026, 6, 1, 10, 0, tzinfo=UTC))
    settings = StateSettings(
        backend=request.param,
        sqlite_path=tmp_path / "contract_limiter.sqlite3",
    )
    return build_state(settings, clock=clock), clock


async def test_rate_limiter_window_edges(
    limiter_bundle: tuple[StateBundle, FrozenClock],
) -> None:
    bundle, clock = limiter_bundle
    rule = RateRule(limit=2, window_s=60)

    d1 = await bundle.limiter.check("user-1", rule)
    assert d1.allowed is True
    assert d1.remaining == 1

    clock.advance(10.0)
    d2 = await bundle.limiter.check("user-1", rule)
    assert d2.allowed is True
    assert d2.remaining == 0

    # 3rd call inside 60s window is rejected with accurate retry_after_s
    clock.advance(5.0)
    d3 = await bundle.limiter.check("user-1", rule)
    assert d3.allowed is False
    assert d3.remaining == 0
    assert pytest.approx(d3.retry_after_s, abs=0.01) == 45.0

    # Once oldest event exits the 60s window, quota opens again
    clock.advance(45.1)
    d4 = await bundle.limiter.check("user-1", rule)
    assert d4.allowed is True


async def test_sqlite_rate_limiter_survives_restart(tmp_path: Path) -> None:
    clock = FrozenClock(datetime(2026, 6, 1, 10, 0, tzinfo=UTC))
    db_file = tmp_path / "limiter_restart.sqlite3"
    settings = StateSettings(backend=StateBackend.SQLITE, sqlite_path=db_file)
    rule = RateRule(limit=1, window_s=60)

    b1 = build_state(settings, clock=clock)
    first = await b1.limiter.check("api-client", rule)
    assert first.allowed is True

    # Simulate process restart by instantiating a new StateBundle on same SQLite file
    b2 = build_state(settings, clock=clock)
    second = await b2.limiter.check("api-client", rule)
    assert second.allowed is False
