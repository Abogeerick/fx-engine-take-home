"""Rate provider tests against a SQLite cache and a fake source.

Asserts the read-flow contract:

    1. cache FRESH -> short-circuit, no fetch
    2. cache stale or missing -> fetch attempted; on success, cache
       upserted and the new value returned at FRESH tier
    3. fetch failure with stale cache -> cached value returned with
       its actual tier (CACHED or STALE_UNUSABLE)
    4. fetch failure with no cache -> RateFetchFailed propagates

Source is a hand-written fake that records calls. The breaker and
singleflight are real instances composed exactly as production
would compose them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from alembic.config import Config as AlembicConfig
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from alembic import command
from app.domain.clock import Clock, FrozenClock
from app.domain.currency import Currency
from app.domain.rate_source import RateFetchFailed
from app.domain.staleness import StalenessTier
from app.infra.rate_provider import (
    CircuitBreaker,
    RateCache,
    RateProvider,
    Singleflight,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config(async_url: str) -> AlembicConfig:
    config = AlembicConfig(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", async_url)
    return config


@pytest.fixture(scope="module")
def sqlite_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    db_path = tmp_path_factory.mktemp("rate_provider") / "test.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    command.upgrade(_alembic_config(async_url), "head")
    yield async_url


@pytest_asyncio.fixture
async def session_factory(
    sqlite_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(sqlite_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        async with factory() as cleanup:
            await cleanup.execute(text("DELETE FROM rates"))
            await cleanup.commit()
        await engine.dispose()


# --- fakes -----------------------------------------------------------------


class FakeSource:
    """In-memory ``RateSource`` with controllable rate map and failure flag."""

    SOURCE_NAME = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple[Currency, Currency]] = []
        self.rates: dict[tuple[Currency, Currency], Decimal] = {}
        self.fail: bool = False

    async def get_rate(
        self,
        *,
        base: Currency,
        quote: Currency,
        now: datetime,
    ) -> tuple[Decimal, datetime, str]:
        self.calls.append((base, quote))
        if self.fail:
            raise RateFetchFailed("fake upstream down")
        rate = self.rates.get((base, quote))
        if rate is None:
            raise RateFetchFailed(f"no fake rate configured for {base}/{quote}")
        return (rate, now, self.SOURCE_NAME)


def _make_provider(
    *,
    source: FakeSource,
    cache: RateCache,
    clock: Clock,
) -> RateProvider:
    breaker = CircuitBreaker(
        failure_threshold=3,
        cooldown=timedelta(seconds=30),
        clock=clock,
    )
    sf: Singleflight[tuple[Currency, Currency], None] = Singleflight(wait_timeout=2.0)
    return RateProvider(
        source=source, cache=cache, circuit_breaker=breaker, singleflight=sf, clock=clock
    )


NOW = datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC)


# --- read flow -------------------------------------------------------------


async def test_fresh_cache_short_circuits_no_upstream_call(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    clock = FrozenClock(start=NOW)
    cache = RateCache(session_factory)
    source = FakeSource()
    source.rates[(Currency.USD, Currency.KES)] = Decimal("130")

    # Pre-populate cache with a fresh entry.
    await cache.upsert(
        base=Currency.USD,
        quote=Currency.KES,
        mid_rate=Decimal("129.50"),
        fetched_at=NOW,
        source="seed",
    )

    provider = _make_provider(source=source, cache=cache, clock=clock)
    info = await provider.get_rate(base=Currency.USD, quote=Currency.KES)

    assert info.mid_rate == Decimal("129.50")
    assert info.tier == StalenessTier.FRESH
    assert source.calls == []  # no upstream call


async def test_cache_miss_triggers_fetch_and_writes_through(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    clock = FrozenClock(start=NOW)
    cache = RateCache(session_factory)
    source = FakeSource()
    source.rates[(Currency.USD, Currency.KES)] = Decimal("130")

    provider = _make_provider(source=source, cache=cache, clock=clock)
    info = await provider.get_rate(base=Currency.USD, quote=Currency.KES)

    assert info.mid_rate == Decimal("130")
    assert info.tier == StalenessTier.FRESH
    assert source.calls == [(Currency.USD, Currency.KES)]

    # Subsequent call with no clock advance hits the cache only.
    info2 = await provider.get_rate(base=Currency.USD, quote=Currency.KES)
    assert source.calls == [(Currency.USD, Currency.KES)]  # unchanged
    assert info2.mid_rate == Decimal("130")


async def test_stale_cache_with_fetch_failure_returns_cached_at_correct_tier(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    clock = FrozenClock(start=NOW)
    cache = RateCache(session_factory)
    source = FakeSource()

    # Seed cache at NOW - 5 minutes -> CACHED tier when classified at NOW.
    seeded_at = NOW - timedelta(minutes=5)
    await cache.upsert(
        base=Currency.USD,
        quote=Currency.KES,
        mid_rate=Decimal("130"),
        fetched_at=seeded_at,
        source="seed",
    )
    source.fail = True  # upstream is down

    provider = _make_provider(source=source, cache=cache, clock=clock)
    info = await provider.get_rate(base=Currency.USD, quote=Currency.KES)

    assert info.mid_rate == Decimal("130")
    assert info.tier == StalenessTier.CACHED
    assert source.calls == [(Currency.USD, Currency.KES)]  # we tried


async def test_stale_unusable_cache_with_fetch_failure_returns_stale_tier(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    clock = FrozenClock(start=NOW)
    cache = RateCache(session_factory)
    source = FakeSource()

    seeded_at = NOW - timedelta(minutes=11)  # past 10 min threshold
    await cache.upsert(
        base=Currency.USD,
        quote=Currency.KES,
        mid_rate=Decimal("130"),
        fetched_at=seeded_at,
        source="seed",
    )
    source.fail = True

    provider = _make_provider(source=source, cache=cache, clock=clock)
    info = await provider.get_rate(base=Currency.USD, quote=Currency.KES)

    # The provider returns the stale value with the tier annotated;
    # the API layer converts STALE_UNUSABLE to HTTP 503.
    assert info.tier == StalenessTier.STALE_UNUSABLE


async def test_no_cache_and_fetch_failure_raises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    clock = FrozenClock(start=NOW)
    cache = RateCache(session_factory)
    source = FakeSource()
    source.fail = True

    provider = _make_provider(source=source, cache=cache, clock=clock)
    with pytest.raises(RateFetchFailed):
        await provider.get_rate(base=Currency.USD, quote=Currency.KES)


# --- breaker integration ---------------------------------------------------


async def test_open_breaker_falls_through_to_cache(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """After repeated failures the breaker opens; subsequent calls
    skip the upstream entirely and return whatever the cache has."""
    clock = FrozenClock(start=NOW)
    cache = RateCache(session_factory)
    source = FakeSource()

    # Seed a slightly-stale cache so we have a fallback.
    await cache.upsert(
        base=Currency.USD,
        quote=Currency.KES,
        mid_rate=Decimal("100"),
        fetched_at=NOW - timedelta(minutes=2),
        source="seed",
    )
    source.fail = True

    provider = _make_provider(source=source, cache=cache, clock=clock)

    # Drive the breaker open (3 consecutive failures).
    for _ in range(3):
        info = await provider.get_rate(base=Currency.USD, quote=Currency.KES)
        assert info.mid_rate == Decimal("100")  # stale fallback
    # 3 upstream attempts were made.
    assert len(source.calls) == 3

    # Breaker is now open. Next call skips upstream.
    info = await provider.get_rate(base=Currency.USD, quote=Currency.KES)
    assert info.mid_rate == Decimal("100")
    assert len(source.calls) == 3  # still 3, no new attempt


# --- singleflight integration ---------------------------------------------


async def test_concurrent_misses_for_same_pair_invoke_source_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    import asyncio

    clock = FrozenClock(start=NOW)
    cache = RateCache(session_factory)
    started = asyncio.Event()
    release = asyncio.Event()
    invocations = 0

    class SlowSource:
        SOURCE_NAME = "slow"

        async def get_rate(self, *, base, quote, now):
            nonlocal invocations
            invocations += 1
            started.set()
            await release.wait()
            return (Decimal("130"), now, self.SOURCE_NAME)

    provider = _make_provider(source=SlowSource(), cache=cache, clock=clock)  # type: ignore[arg-type]

    tasks = [
        asyncio.create_task(provider.get_rate(base=Currency.USD, quote=Currency.KES))
        for _ in range(8)
    ]
    await started.wait()
    release.set()
    results = await asyncio.gather(*tasks)

    assert invocations == 1
    assert all(r.mid_rate == Decimal("130") for r in results)
