"""Clock protocol and reference implementations.

Per CLAUDE.md hard rule §4.6: domain code never reads
``datetime.utcnow()`` directly. Time enters the domain through a
``Clock`` dependency so tests can advance time deterministically.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Return the current time as a tz-aware UTC datetime."""
        ...


class SystemClock:
    """Production clock backed by the OS wall clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """Test clock that returns a fixed instant until ``tick`` advances it.

    The constructor requires a tz-aware datetime and normalises it to
    UTC, so tests cannot accidentally compare naive and aware times.
    """

    def __init__(self, *, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("FrozenClock requires a tz-aware datetime")
        self._t = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._t

    def tick(self, delta: timedelta) -> None:
        self._t = self._t + delta
