"""SQLite-tier repository tests.

Schema applied once per module via Alembic (which exercises the same
migration that runs on Postgres in production); per-test cleanup
truncates the three tables. Covers CRUD, upsert semantics, freshness
tier transitions across a FrozenClock advance, and the DB-level CHECK
constraints.

The Alembic upgrade->downgrade roundtrip lives as a sync test so the
asyncio.run() inside Alembic's online mode does not collide with
pytest-asyncio's event loop.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic.config import Config as AlembicConfig
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from alembic import command
from app.domain.clock import FrozenClock
from app.domain.currency import Currency
from app.domain.money import Money
from app.domain.staleness import StalenessTier
from app.infra.models import Balance as BalanceTable
from app.infra.repositories import (
    BalanceRepository,
    CustomerRepository,
    InsufficientBalance,
    RateRepository,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config(async_url: str) -> AlembicConfig:
    config = AlembicConfig(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", async_url)
    return config


@pytest.fixture(scope="module")
def sqlite_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    db_path = tmp_path_factory.mktemp("sqlite") / "test.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    command.upgrade(_alembic_config(async_url), "head")
    yield async_url


@pytest_asyncio.fixture
async def session(sqlite_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(sqlite_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as s:
            yield s
    finally:
        # Per-test cleanup: SQLite has no TRUNCATE, so DELETE FROM each
        # table. Order matters because of the FK from balances to customers.
        async with factory() as cleanup:
            await cleanup.execute(text("DELETE FROM rates"))
            await cleanup.execute(text("DELETE FROM balances"))
            await cleanup.execute(text("DELETE FROM customers"))
            await cleanup.commit()
        await engine.dispose()


# --- migration roundtrip ----------------------------------------------------


def test_alembic_upgrade_then_downgrade_on_sqlite(tmp_path: Path) -> None:
    """AC #1: migration applies cleanly and reverses cleanly on SQLite."""
    db_path = tmp_path / "roundtrip.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    config = _alembic_config(async_url)

    # contextlib.closing because sqlite3.Connection's `with` block manages
    # the *transaction*, not the connection lifecycle -- the file stays
    # open without an explicit close, which trips strict ResourceWarning.
    command.upgrade(config, "head")
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    tables = {r[0] for r in rows}
    assert {"customers", "balances", "rates", "alembic_version"} <= tables

    command.downgrade(config, "base")
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    tables = {r[0] for r in rows}
    assert "customers" not in tables
    assert "balances" not in tables
    assert "rates" not in tables


# --- customer repository ----------------------------------------------------


async def test_customer_create_and_get(session: AsyncSession) -> None:
    cid = uuid4()
    await CustomerRepository.create(session, cid)
    await session.commit()

    retrieved = await CustomerRepository.get(session, cid)
    assert retrieved is not None
    assert retrieved.id == cid
    assert retrieved.created_at.tzinfo is not None


async def test_customer_get_missing_returns_none(session: AsyncSession) -> None:
    assert await CustomerRepository.get(session, uuid4()) is None


# --- balance repository -----------------------------------------------------


async def test_balance_credit_creates_row(session: AsyncSession) -> None:
    cid = uuid4()
    await CustomerRepository.create(session, cid)

    await BalanceRepository.credit(
        session, cid, Money(amount=Decimal("100.00"), currency=Currency.USD)
    )
    await session.commit()

    bals = await BalanceRepository.get_all(session, cid)
    assert len(bals) == 1
    assert bals[0].currency == "USD"
    assert bals[0].amount == Decimal("100.00")


async def test_balance_credit_accumulates(session: AsyncSession) -> None:
    cid = uuid4()
    await CustomerRepository.create(session, cid)
    await BalanceRepository.credit(
        session, cid, Money(amount=Decimal("100"), currency=Currency.USD)
    )
    await BalanceRepository.credit(session, cid, Money(amount=Decimal("50"), currency=Currency.USD))
    await session.commit()

    bals = await BalanceRepository.get_all(session, cid)
    assert bals[0].amount == Decimal("150")


async def test_balance_debit_succeeds(session: AsyncSession) -> None:
    cid = uuid4()
    await CustomerRepository.create(session, cid)
    await BalanceRepository.credit(
        session, cid, Money(amount=Decimal("100"), currency=Currency.USD)
    )

    await BalanceRepository.debit(session, cid, Money(amount=Decimal("30"), currency=Currency.USD))
    await session.commit()

    bals = await BalanceRepository.get_all(session, cid)
    assert bals[0].amount == Decimal("70")


async def test_balance_debit_insufficient_raises(session: AsyncSession) -> None:
    cid = uuid4()
    await CustomerRepository.create(session, cid)
    await BalanceRepository.credit(session, cid, Money(amount=Decimal("10"), currency=Currency.USD))

    with pytest.raises(InsufficientBalance):
        await BalanceRepository.debit(
            session, cid, Money(amount=Decimal("100"), currency=Currency.USD)
        )


async def test_balance_get_for_update_returns_row(session: AsyncSession) -> None:
    cid = uuid4()
    await CustomerRepository.create(session, cid)
    await BalanceRepository.credit(session, cid, Money(amount=Decimal("50"), currency=Currency.USD))
    await session.commit()

    row = await BalanceRepository.get_for_update(session, cid, Currency.USD)
    assert row is not None
    assert row.amount == Decimal("50")


async def test_balance_get_for_update_returns_none_when_absent(
    session: AsyncSession,
) -> None:
    row = await BalanceRepository.get_for_update(session, uuid4(), Currency.USD)
    assert row is None


async def test_balance_credit_negative_rejected_by_repo(session: AsyncSession) -> None:
    cid = uuid4()
    await CustomerRepository.create(session, cid)
    with pytest.raises(ValueError, match="positive"):
        await BalanceRepository.credit(
            session, cid, Money(amount=Decimal("-1"), currency=Currency.USD)
        )


async def test_balance_check_constraint_blocks_negative_insert(
    session: AsyncSession,
) -> None:
    """The DB CHECK constraint enforces non-negative even when SQL bypasses
    the repository's own guards. SA Core ``insert`` is used so the Uuid
    type adapter binds the customer_id correctly across dialects."""
    cid = uuid4()
    await CustomerRepository.create(session, cid)
    await session.commit()

    stmt = insert(BalanceTable).values(customer_id=cid, currency="USD", amount=Decimal("-1"))
    with pytest.raises(IntegrityError):
        await session.execute(stmt)
        await session.commit()


async def test_balance_check_constraint_blocks_unsupported_currency(
    session: AsyncSession,
) -> None:
    cid = uuid4()
    await CustomerRepository.create(session, cid)
    await session.commit()

    stmt = insert(BalanceTable).values(customer_id=cid, currency="XYZ", amount=Decimal("10"))
    with pytest.raises(IntegrityError):
        await session.execute(stmt)
        await session.commit()


# --- rate repository --------------------------------------------------------


async def test_rate_upsert_inserts_then_updates(session: AsyncSession) -> None:
    t0 = datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC)
    await RateRepository.upsert(
        session,
        base=Currency.USD,
        quote=Currency.KES,
        mid_rate=Decimal("130.00"),
        fetched_at=t0,
        source="test",
    )
    await session.commit()

    got = await RateRepository.get(session, base=Currency.USD, quote=Currency.KES)
    assert got is not None
    assert got[0] == Decimal("130.00")

    t1 = t0 + timedelta(seconds=30)
    await RateRepository.upsert(
        session,
        base=Currency.USD,
        quote=Currency.KES,
        mid_rate=Decimal("131.50"),
        fetched_at=t1,
        source="test2",
    )
    await session.commit()

    got2 = await RateRepository.get(session, base=Currency.USD, quote=Currency.KES)
    assert got2 is not None
    assert got2[0] == Decimal("131.50")


async def test_rate_freshness_tier_transitions(session: AsyncSession) -> None:
    start = datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC)
    await RateRepository.upsert(
        session,
        base=Currency.USD,
        quote=Currency.KES,
        mid_rate=Decimal("130.00"),
        fetched_at=start,
        source="test",
    )
    await session.commit()

    clock = FrozenClock(start=start)
    assert (
        await RateRepository.freshness_tier(
            session, base=Currency.USD, quote=Currency.KES, now=clock.now()
        )
        == StalenessTier.FRESH
    )

    clock.tick(timedelta(seconds=60))  # at boundary -> still fresh
    assert (
        await RateRepository.freshness_tier(
            session, base=Currency.USD, quote=Currency.KES, now=clock.now()
        )
        == StalenessTier.FRESH
    )

    clock.tick(timedelta(seconds=1))  # 61s -> cached
    assert (
        await RateRepository.freshness_tier(
            session, base=Currency.USD, quote=Currency.KES, now=clock.now()
        )
        == StalenessTier.CACHED
    )

    clock = FrozenClock(start=start)
    clock.tick(timedelta(minutes=10))  # at boundary -> still cached
    assert (
        await RateRepository.freshness_tier(
            session, base=Currency.USD, quote=Currency.KES, now=clock.now()
        )
        == StalenessTier.CACHED
    )

    clock.tick(timedelta(seconds=1))  # 10:01 -> stale_unusable
    assert (
        await RateRepository.freshness_tier(
            session, base=Currency.USD, quote=Currency.KES, now=clock.now()
        )
        == StalenessTier.STALE_UNUSABLE
    )


async def test_rate_freshness_tier_missing_returns_none(session: AsyncSession) -> None:
    clock = FrozenClock(start=datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC))
    tier = await RateRepository.freshness_tier(
        session, base=Currency.USD, quote=Currency.KES, now=clock.now()
    )
    assert tier is None


async def test_rate_upsert_rejects_non_positive(session: AsyncSession) -> None:
    t0 = datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="positive"):
        await RateRepository.upsert(
            session,
            base=Currency.USD,
            quote=Currency.KES,
            mid_rate=Decimal("0"),
            fetched_at=t0,
            source="test",
        )


async def test_rate_upsert_rejects_naive_datetime(session: AsyncSession) -> None:
    naive = datetime(2026, 5, 7, 12, 0, 0)
    with pytest.raises(ValueError, match="tz-aware"):
        await RateRepository.upsert(
            session,
            base=Currency.USD,
            quote=Currency.KES,
            mid_rate=Decimal("130"),
            fetched_at=naive,
            source="test",
        )
