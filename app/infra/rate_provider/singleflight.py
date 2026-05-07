"""Async singleflight coalescer -- one inflight call per key.

Per SPEC §8: when N callers ask for the same key concurrently while
a fetch is in progress, exactly one fetch happens; all callers
receive the same result. Waiters that exceed ``wait_timeout``
(default 5s) raise ``TimeoutError``; the in-flight task continues
unaffected so subsequent callers can still receive its result.

asyncio-only. The ``asyncio.Lock`` guards the in-flight map; the
lock is **never** held across an ``await fn()``, so the coalescer
itself never blocks the event loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Hashable
from typing import Generic, TypeVar

K = TypeVar("K", bound=Hashable)
T = TypeVar("T")


class Singleflight(Generic[K, T]):
    def __init__(self, *, wait_timeout: float = 5.0) -> None:
        self._in_flight: dict[K, asyncio.Future[T]] = {}
        # Keep strong references to spawned tasks so they cannot be
        # garbage-collected mid-execution (asyncio docs note this is
        # a real footgun with create_task).
        self._tasks: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()
        self._wait_timeout = wait_timeout

    async def do(self, key: K, fn: Callable[[], Awaitable[T]]) -> T:
        """Coalesce concurrent calls for ``key``.

        If a fetch is already in flight for ``key``, the caller awaits
        the same future. Otherwise a new fetch is spawned and the
        caller becomes the first waiter on it.
        """
        async with self._lock:
            existing = self._in_flight.get(key)
            if existing is not None:
                fut = existing
            else:
                loop = asyncio.get_running_loop()
                fut = loop.create_future()
                self._in_flight[key] = fut
                # Spawn a separate task to do the work so cancellation
                # of the originating caller (e.g. wait_for timeout)
                # doesn't kill the underlying fetch.
                task = asyncio.create_task(self._run(key, fn, fut))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout=self._wait_timeout)
        except TimeoutError:
            # Don't pop the in-flight entry: the underlying task still
            # owns the future and will resolve it for any other waiter.
            raise

    async def _run(
        self,
        key: K,
        fn: Callable[[], Awaitable[T]],
        fut: asyncio.Future[T],
    ) -> None:
        try:
            result = await fn()
        except BaseException as exc:
            if not fut.done():
                fut.set_exception(exc)
        else:
            if not fut.done():
                fut.set_result(result)
        finally:
            async with self._lock:
                # Only pop if this is still the same future under the
                # key (guards against a future-replacing race).
                current = self._in_flight.get(key)
                if current is fut:
                    self._in_flight.pop(key, None)
