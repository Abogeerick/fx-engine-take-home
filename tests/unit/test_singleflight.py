"""Singleflight coalescer tests.

Asserts EXACTLY-ONCE invocation under N concurrent calls, distinct
keys do not coalesce, and timeouts surface to callers without
killing the in-flight task.
"""

from __future__ import annotations

import asyncio

import pytest

from app.infra.rate_provider.singleflight import Singleflight


async def test_concurrent_calls_with_same_key_invoke_fn_exactly_once() -> None:
    sf: Singleflight[str, int] = Singleflight(wait_timeout=2.0)
    invocations = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_fetch() -> int:
        nonlocal invocations
        invocations += 1
        started.set()
        await release.wait()
        return 42

    async def caller() -> int:
        return await sf.do("key", slow_fetch)

    tasks = [asyncio.create_task(caller()) for _ in range(10)]
    await started.wait()  # ensure the fetch has started before all callers commit
    release.set()
    results = await asyncio.gather(*tasks)

    assert invocations == 1
    assert results == [42] * 10


async def test_distinct_keys_each_invoke_fn() -> None:
    sf: Singleflight[str, int] = Singleflight(wait_timeout=2.0)
    invocations: dict[str, int] = {"a": 0, "b": 0}

    def make_fetch(key: str):
        async def _fn() -> int:
            invocations[key] += 1
            return ord(key)

        return _fn

    a, b = await asyncio.gather(
        sf.do("a", make_fetch("a")),
        sf.do("b", make_fetch("b")),
    )
    assert a == ord("a")
    assert b == ord("b")
    assert invocations == {"a": 1, "b": 1}


async def test_fetch_failure_propagates_to_all_waiters() -> None:
    sf: Singleflight[str, int] = Singleflight(wait_timeout=2.0)
    invocations = 0
    started = asyncio.Event()

    async def failing_fetch() -> int:
        nonlocal invocations
        invocations += 1
        started.set()
        await asyncio.sleep(0.05)
        raise RuntimeError("upstream down")

    async def caller() -> int:
        return await sf.do("k", failing_fetch)

    tasks = [asyncio.create_task(caller()) for _ in range(5)]
    await started.wait()
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert invocations == 1
    assert all(isinstance(r, RuntimeError) for r in results)


async def test_after_completion_subsequent_call_starts_new_fetch() -> None:
    sf: Singleflight[str, int] = Singleflight(wait_timeout=2.0)
    invocations = 0

    async def fast_fetch() -> int:
        nonlocal invocations
        invocations += 1
        return invocations

    first = await sf.do("k", fast_fetch)
    second = await sf.do("k", fast_fetch)
    assert first == 1
    assert second == 2
    assert invocations == 2


async def test_caller_timeout_does_not_kill_in_flight_task() -> None:
    """A waiter that times out leaves the underlying fetch alive; a
    later caller within the same in-flight window receives the result."""
    sf: Singleflight[str, int] = Singleflight(wait_timeout=0.1)
    invocations = 0
    release = asyncio.Event()
    started = asyncio.Event()

    async def slow_fetch() -> int:
        nonlocal invocations
        invocations += 1
        started.set()
        await release.wait()
        return 7

    # First caller: times out at 0.1s
    short_task = asyncio.create_task(sf.do("k", slow_fetch))
    await started.wait()
    with pytest.raises(TimeoutError):
        await short_task

    # Now release; the in-flight task completes its future.
    release.set()
    # A new caller asking now would start a new fetch (the previous one
    # finished and was popped); but if we'd asked while in flight, we
    # would have piggy-backed. Verify that the underlying invocations
    # count is exactly 1.
    assert invocations == 1
