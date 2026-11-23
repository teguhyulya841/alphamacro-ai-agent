"""Clock abstractions for deterministic time injection across the codebase."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import AwareDatetime


class SystemClock:
    """Production clock returning timezone-aware UTC timestamps."""

    def now(self) -> AwareDatetime:
        return datetime.now(UTC)


class FrozenClock:
    """Deterministic clock for unit and contract tests."""

    def __init__(self, initial: AwareDatetime | None = None) -> None:
        self._now: AwareDatetime = initial or datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> AwareDatetime:
        return self._now

    def advance(self, seconds: float) -> AwareDatetime:
        """Move the clock forward by ``seconds`` and return the new timestamp."""
        if seconds < 0:
            raise ValueError("Cannot advance FrozenClock backwards; use set() instead.")
        self._now = self._now + timedelta(seconds=seconds)
        return self._now

    def set(self, timestamp: AwareDatetime) -> AwareDatetime:
        """Set the clock to an explicit timezone-aware timestamp."""
        if timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        self._now = timestamp
        return self._now
