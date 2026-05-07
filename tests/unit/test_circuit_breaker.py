"""Circuit breaker state-machine tests.

Tested via direct ``cb.call(fn)`` with a ``FrozenClock`` -- no HTTP,
no httpx mocking. The wrapped function is just a counter or a
parametrised raiser; the only thing under test is the state machine.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.domain.clock import FrozenClock
from app.infra.rate_provider.circuit_breaker import (
    CircuitBreaker,
    CircuitState,
    OpenCircuitError,
)


def _new_cb(
    *,
    threshold: int = 3,
    cooldown_s: int = 30,
    start: datetime = datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC),
) -> tuple[CircuitBreaker, FrozenClock]:
    clock = FrozenClock(start=start)
    cb = CircuitBreaker(
        failure_threshold=threshold,
        cooldown=timedelta(seconds=cooldown_s),
        clock=clock,
    )
    return cb, clock


async def _ok() -> str:
    return "ok"


def _failing(message: str = "boom"):
    async def _fn() -> str:
        raise RuntimeError(message)

    return _fn


# --- baseline ---------------------------------------------------------------


async def test_initial_state_is_closed() -> None:
    cb, _ = _new_cb()
    assert cb.state == CircuitState.CLOSED


async def test_closed_forwards_call_and_returns_result() -> None:
    cb, _ = _new_cb()
    assert await cb.call(_ok) == "ok"
    assert cb.state == CircuitState.CLOSED


# --- closed -> open ---------------------------------------------------------


async def test_closed_stays_closed_below_threshold() -> None:
    cb, _ = _new_cb(threshold=3)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await cb.call(_failing())
    assert cb.state == CircuitState.CLOSED


async def test_closed_opens_after_threshold_consecutive_failures() -> None:
    cb, _ = _new_cb(threshold=3)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await cb.call(_failing())
    assert cb.state == CircuitState.OPEN


async def test_failure_count_resets_on_success() -> None:
    cb, _ = _new_cb(threshold=3)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await cb.call(_failing())
    await cb.call(_ok)  # reset
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await cb.call(_failing())
    assert cb.state == CircuitState.CLOSED  # only 2 consecutive after reset


# --- open ------------------------------------------------------------------


async def test_open_rejects_calls_without_invoking_fn() -> None:
    cb, _ = _new_cb(threshold=1)
    with pytest.raises(RuntimeError):
        await cb.call(_failing())
    assert cb.state == CircuitState.OPEN

    invocations = 0

    async def counting() -> str:
        nonlocal invocations
        invocations += 1
        return "ok"

    with pytest.raises(OpenCircuitError):
        await cb.call(counting)
    assert invocations == 0


async def test_open_does_not_transition_before_cooldown_elapses() -> None:
    cb, clock = _new_cb(threshold=1, cooldown_s=30)
    with pytest.raises(RuntimeError):
        await cb.call(_failing())
    assert cb.state == CircuitState.OPEN

    clock.tick(timedelta(seconds=29))
    with pytest.raises(OpenCircuitError):
        await cb.call(_ok)
    assert cb.state == CircuitState.OPEN


# --- open -> half_open -> closed -------------------------------------------


async def test_open_transitions_to_half_open_after_cooldown_then_closed_on_success() -> None:
    cb, clock = _new_cb(threshold=1, cooldown_s=30)
    with pytest.raises(RuntimeError):
        await cb.call(_failing())
    assert cb.state == CircuitState.OPEN

    clock.tick(timedelta(seconds=30))  # exactly at cooldown boundary
    result = await cb.call(_ok)  # this is the trial
    assert result == "ok"
    assert cb.state == CircuitState.CLOSED


# --- open -> half_open -> open on trial failure ----------------------------


async def test_half_open_reopens_on_trial_failure() -> None:
    cb, clock = _new_cb(threshold=1, cooldown_s=30)
    with pytest.raises(RuntimeError):
        await cb.call(_failing())
    clock.tick(timedelta(seconds=30))

    with pytest.raises(RuntimeError):
        await cb.call(_failing())
    assert cb.state == CircuitState.OPEN

    # The cooldown reference resets on a failed trial -- subsequent
    # ticks below the new cooldown still see OPEN.
    clock.tick(timedelta(seconds=29))
    with pytest.raises(OpenCircuitError):
        await cb.call(_ok)
    assert cb.state == CircuitState.OPEN


# --- concurrency: half_open admits exactly one trial -----------------------


async def test_half_open_admits_only_one_concurrent_trial() -> None:
    cb, clock = _new_cb(threshold=1, cooldown_s=30)
    with pytest.raises(RuntimeError):
        await cb.call(_failing())
    clock.tick(timedelta(seconds=30))
    # Now state will transition to HALF_OPEN on the next call.

    invocations = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_ok() -> str:
        nonlocal invocations
        invocations += 1
        started.set()
        await release.wait()
        return "ok"

    # Trial caller -- holds HALF_OPEN open while waiting on `release`.
    trial = asyncio.create_task(cb.call(slow_ok))
    await started.wait()

    # Concurrent callers must be rejected as if the breaker were OPEN.
    with pytest.raises(OpenCircuitError):
        await cb.call(_ok)
    with pytest.raises(OpenCircuitError):
        await cb.call(_ok)

    release.set()
    assert await trial == "ok"
    assert invocations == 1
    assert cb.state == CircuitState.CLOSED
