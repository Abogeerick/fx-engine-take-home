"""``RateRefreshScheduler`` integration test against real Postgres.

Asserts the scheduler:
  * spawns a background task on ``start()``
  * iterates the configured pairs and writes through to the rates
    table
  * survives a single-pair failure (one bad pair must not kill the
    loop)
  * stops cleanly on ``stop()`` (cancels the task, awaits cleanup)

The source is a controllable fake so the test does not depend on
exchangeratesapi.io being reachable.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.clock import SystemClock
from app.domain.currency import Currency
from app.domain.rate_source import RateFetchFailed
from app.infra.rate_provider import (
    CircuitBreaker,
    RateCache,
    RateProvider,
    RateRefreshScheduler,
    Singleflight,
)
from app.infra.repositories import RateRepository


class _ControllableSource:
    SOURCE_NAME = "scheduler-test"

    def __init__(self) -> None:
        self.rates: dict[tuple[Currency, Currency], Decimal] = {}
        self.fail_pairs: set[tuple[Currency, Currency]] = set()
        self.call_count = 0

    async def get_rate(
        self,
        *,
        base: Currency,
        quote: Currency,
        now: datetime,
    ) -> tuple[Decimal, datetime, str]:
        self.call_count += 1
        if (base, quote) in self.fail_pairs:
            raise RateFetchFailed(f"{base.value}/{quote.value} simulated failure")
        rate = self.rates.get((base, quote))
        if rate is None:
            raise RateFetchFailed("no rate configured")
        return (rate, now, self.SOURCE_NAME)


def _make_provider_and_scheduler(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    source: _ControllableSource,
    pairs: list[tuple[Currency, Currency]],
    interval_s: float,
) -> tuple[RateProvider, RateRefreshScheduler]:
    clock = SystemClock()
    cache = RateCache(session_factory)
    breaker = CircuitBreaker(
        failure_threshold=10,  # high so breaker doesn't open during the brief test
        cooldown=timedelta(seconds=30),
        clock=clock,
    )
    sf: Singleflight[tuple[Currency, Currency], None] = Singleflight(wait_timeout=2.0)
    provider = RateProvider(
        source=source, cache=cache, circuit_breaker=breaker, singleflight=sf, clock=clock
    )
    scheduler = RateRefreshScheduler(
        provider=provider,
        pairs=pairs,
        interval=timedelta(seconds=interval_s),
    )
    return provider, scheduler


async def test_scheduler_writes_all_pairs_to_rates_table(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    pairs = [
        (Currency.USD, Currency.KES),
        (Currency.USD, Currency.NGN),
        (Currency.EUR, Currency.KES),
    ]
    source = _ControllableSource()
    source.rates = {
        (Currency.USD, Currency.KES): Decimal("130"),
        (Currency.USD, Currency.NGN): Decimal("1500"),
        (Currency.EUR, Currency.KES): Decimal("140"),
    }

    _, scheduler = _make_provider_and_scheduler(
        session_factory=session_factory,
        source=source,
        pairs=pairs,
        interval_s=10.0,  # large interval; we only need the first pass
    )

    await scheduler.start()
    # Allow one full pass to complete; pairs are sequential.
    # Three pairs fetched -> ~ a handful of ms via SystemClock.
    for _ in range(40):
        await asyncio.sleep(0.05)
        async with session_factory() as session:
            count = 0
            for base, quote in pairs:
                row = await RateRepository.get(session, base=base, quote=quote)
                if row is not None:
                    count += 1
        if count == len(pairs):
            break
    await scheduler.stop()

    async with session_factory() as session:
        for base, quote in pairs:
            row = await RateRepository.get(session, base=base, quote=quote)
            assert row is not None, f"missing row for {base.value}/{quote.value}"
            assert row[0] == source.rates[(base, quote)]


async def test_scheduler_continues_after_single_pair_failure(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    pairs = [
        (Currency.USD, Currency.KES),
        (Currency.USD, Currency.NGN),  # this one will fail
        (Currency.EUR, Currency.KES),
    ]
    source = _ControllableSource()
    source.rates = {
        (Currency.USD, Currency.KES): Decimal("130"),
        (Currency.EUR, Currency.KES): Decimal("140"),
    }
    source.fail_pairs = {(Currency.USD, Currency.NGN)}

    _, scheduler = _make_provider_and_scheduler(
        session_factory=session_factory,
        source=source,
        pairs=pairs,
        interval_s=10.0,
    )

    await scheduler.start()
    # Wait for the two good pairs to land.
    for _ in range(40):
        await asyncio.sleep(0.05)
        async with session_factory() as session:
            usd_kes = await RateRepository.get(session, base=Currency.USD, quote=Currency.KES)
            eur_kes = await RateRepository.get(session, base=Currency.EUR, quote=Currency.KES)
        if usd_kes is not None and eur_kes is not None:
            break
    await scheduler.stop()

    # The good pairs were written even though USD/NGN failed.
    async with session_factory() as session:
        assert (
            await RateRepository.get(session, base=Currency.USD, quote=Currency.KES)
        ) is not None
        assert (
            await RateRepository.get(session, base=Currency.EUR, quote=Currency.KES)
        ) is not None
        assert (await RateRepository.get(session, base=Currency.USD, quote=Currency.NGN)) is None


async def test_scheduler_stop_is_idempotent_and_clean(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    source = _ControllableSource()
    source.rates = {(Currency.USD, Currency.KES): Decimal("130")}

    _, scheduler = _make_provider_and_scheduler(
        session_factory=session_factory,
        source=source,
        pairs=[(Currency.USD, Currency.KES)],
        interval_s=10.0,
    )

    await scheduler.start()
    await asyncio.sleep(0.1)
    await scheduler.stop()
    # Calling stop twice is a no-op, not an error.
    await scheduler.stop()
