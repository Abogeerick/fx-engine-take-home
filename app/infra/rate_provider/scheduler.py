"""Background refresh scheduler.

Iterates the configured pairs once per ``interval`` and asks the
``RateProvider`` to refresh each. The provider's own cache + breaker
+ singleflight machinery handles upstream failures; the scheduler
just kicks the loop.

asyncio-only: ``start()`` spawns ``asyncio.create_task``; ``stop()``
sets a stop event, cancels the task, and awaits its cleanup. There
is no thread.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING

import structlog

from app.domain.currency import Currency

if TYPE_CHECKING:
    from app.infra.rate_provider import RateProvider

log = structlog.get_logger(__name__)


class RateRefreshScheduler:
    def __init__(
        self,
        *,
        provider: RateProvider,
        pairs: list[tuple[Currency, Currency]],
        interval: timedelta = timedelta(seconds=60),
    ) -> None:
        self._provider = provider
        self._pairs = pairs
        self._interval = interval
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("scheduler already started")
        self._stopped.clear()
        self._task = asyncio.create_task(self._loop(), name="rate-refresh-loop")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stopped.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _loop(self) -> None:
        while not self._stopped.is_set():
            for base, quote in self._pairs:
                if self._stopped.is_set():
                    return
                try:
                    await self._provider.get_rate(base=base, quote=quote)
                except Exception as exc:
                    # One bad pair does not kill the loop; tier classification
                    # at the API layer will surface stale-unusable to clients.
                    log.warning(
                        "rate.refresh.pair_failed",
                        base=base.value,
                        quote=quote.value,
                        error=str(exc),
                    )

            try:
                await asyncio.wait_for(
                    self._stopped.wait(),
                    timeout=self._interval.total_seconds(),
                )
            except TimeoutError:
                continue
