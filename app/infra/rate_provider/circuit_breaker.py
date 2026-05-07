"""Async circuit breaker with closed / open / half_open states.

Per SPEC §8: after 3 consecutive failures the breaker opens for 30
seconds; one trial call in HALF_OPEN closes it on success or re-opens
it on failure.

State machine
=============

::

                +---------+
        success | CLOSED  | <-------------+
        ------> |         | --------+     |
                +---------+         |     | success
                     | failure_threshold  |
                     v reached            |
                +---------+               |
                |  OPEN   |               |
                +---------+               |
                     | cooldown elapsed   |
                     v                    |
                +-----------+ failure -- back to OPEN with reset cooldown
                | HALF_OPEN |
                +-----------+

Concurrency
===========

State mutations happen under an ``asyncio.Lock``. The lock is
**released** before invoking the wrapped function, so callers in
the CLOSED state run with full concurrency. In HALF_OPEN, only one
call (the trial) is admitted at a time -- concurrent callers see
``OpenCircuitError`` until the trial completes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TypeVar

from app.domain.clock import Clock

T = TypeVar("T")


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class OpenCircuitError(Exception):
    """The breaker is OPEN (or HALF_OPEN with a trial already in flight).

    Callers should treat this as "skip the upstream this time"; do
    not retry inside the same logical request.
    """


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        cooldown: timedelta = timedelta(seconds=30),
        clock: Clock,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        self._failure_threshold = failure_threshold
        self._cooldown = cooldown
        self._clock = clock
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: datetime | None = None
        self._half_open_in_progress = False
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        """Current state. Safe to read without the lock for inspection."""
        return self._state

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        # -- phase 1: state check (locked) --------------------------------
        async with self._lock:
            self._maybe_transition_open_to_half_open()

            if self._state == CircuitState.OPEN:
                raise OpenCircuitError("circuit is open; upstream call skipped")

            entered_state = self._state
            if entered_state == CircuitState.HALF_OPEN:
                if self._half_open_in_progress:
                    # Another caller is the trial; reject this one.
                    raise OpenCircuitError("circuit half_open with trial in flight; call rejected")
                self._half_open_in_progress = True

        # -- phase 2: invoke fn (lock released) ---------------------------
        try:
            result = await fn()
        except Exception:
            async with self._lock:
                self._on_failure(entered_state)
            raise

        async with self._lock:
            self._on_success(entered_state)
        return result

    def _maybe_transition_open_to_half_open(self) -> None:
        if (
            self._state == CircuitState.OPEN
            and self._opened_at is not None
            and self._clock.now() - self._opened_at >= self._cooldown
        ):
            self._state = CircuitState.HALF_OPEN
            self._half_open_in_progress = False

    def _on_failure(self, entered_state: CircuitState) -> None:
        if entered_state == CircuitState.HALF_OPEN:
            self._state = CircuitState.OPEN
            self._opened_at = self._clock.now()
            self._consecutive_failures = 0
            self._half_open_in_progress = False
        elif entered_state == CircuitState.CLOSED:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._failure_threshold:
                self._state = CircuitState.OPEN
                self._opened_at = self._clock.now()
                self._consecutive_failures = 0

    def _on_success(self, entered_state: CircuitState) -> None:
        if entered_state == CircuitState.HALF_OPEN:
            self._state = CircuitState.CLOSED
            self._consecutive_failures = 0
            self._half_open_in_progress = False
        elif entered_state == CircuitState.CLOSED:
            self._consecutive_failures = 0
