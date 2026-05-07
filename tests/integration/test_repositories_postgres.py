"""Integration tests requiring Postgres.

Three things this layer covers that SQLite cannot:

  * NUMERIC(20, 8) round-trip across the asyncpg wire -- catches
    silent precision loss if the type adapter regresses.
  * CHECK constraints enforced by Postgres on direct SQL writes
    that bypass the repository.
  * SELECT ... FOR UPDATE actually blocks a second concurrent
    session until the first commits. SQLite serialises writers
    at the file level, so the locking semantics are not
    distinguishable there.

The blocking test uses asyncio timestamps instead of an explicit
sleep-and-check pattern -- it asserts that session B's commit
happens *no earlier than* session A's hold elapses.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.currency import Currency
from app.domain.money import Money
from app.infra.models import Balance as BalanceTable
from app.infra.repositories import (
    BalanceRepository,
    CustomerRepository,
    RateRepository,
)


async def test_numeric_round_trip_preserves_precision(session: AsyncSession) -> None:
    cid = uuid4()
    await CustomerRepository.create(session, cid)

    amt = Decimal("1234.12345678")  # 8 fractional digits = the schema ceiling
    await BalanceRepository.credit(session, cid, Money(amount=amt, currency=Currency.KES))
    await session.commit()

    bals = await BalanceRepository.get_all(session, cid)
    assert len(bals) == 1
    assert bals[0].amount == amt


async def test_rate_numeric_round_trip(session: AsyncSession) -> None:
    t0 = datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC)
    rate = Decimal("129.87654321")
    await RateRepository.upsert(
        session,
        base=Currency.USD,
        quote=Currency.KES,
        mid_rate=rate,
        fetched_at=t0,
        source="postgres-test",
    )
    await session.commit()

    got = await RateRepository.get(session, base=Currency.USD, quote=Currency.KES)
    assert got is not None
    assert got[0] == rate
    assert got[1] == t0


async def test_check_constraint_rejects_negative_balance_write(
    session: AsyncSession,
) -> None:
    cid = uuid4()
    await CustomerRepository.create(session, cid)
    await session.commit()

    stmt = insert(BalanceTable).values(customer_id=cid, currency="USD", amount=Decimal("-1"))
    with pytest.raises(IntegrityError):
        await session.execute(stmt)
        await session.commit()


async def test_check_constraint_rejects_unsupported_currency(
    session: AsyncSession,
) -> None:
    cid = uuid4()
    await CustomerRepository.create(session, cid)
    await session.commit()

    stmt = insert(BalanceTable).values(customer_id=cid, currency="XYZ", amount=Decimal("100"))
    with pytest.raises(IntegrityError):
        await session.execute(stmt)
        await session.commit()


async def test_for_update_blocks_concurrent_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    cid = uuid4()
    async with session_factory() as setup:
        await CustomerRepository.create(setup, cid)
        await BalanceRepository.credit(
            setup, cid, Money(amount=Decimal("100"), currency=Currency.USD)
        )
        await setup.commit()

    hold_seconds = 0.5
    loop = asyncio.get_event_loop()

    async def session_a() -> float:
        async with session_factory() as s:
            await BalanceRepository.get_for_update(s, cid, Currency.USD)
            await asyncio.sleep(hold_seconds)
            await s.commit()
        return loop.time()

    async def session_b() -> float:
        # Start slightly after A so A acquires the lock first.
        await asyncio.sleep(0.05)
        async with session_factory() as s:
            await BalanceRepository.get_for_update(s, cid, Currency.USD)
            await s.commit()
        return loop.time()

    start = loop.time()
    _, b_done = await asyncio.gather(session_a(), session_b())

    elapsed = b_done - start
    # B must complete at or after A's hold elapses; allow 10% jitter.
    assert elapsed >= hold_seconds * 0.9, (
        f"Session B finished after {elapsed:.3f}s; "
        f"expected at least {hold_seconds * 0.9:.3f}s "
        f"(FOR UPDATE did not appear to block)"
    )


async def test_concurrent_credit_and_debit_serialise(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two transactions racing on the same balance row produce a final
    state consistent with a serial schedule (initial + credit - debit)."""
    cid = uuid4()
    async with session_factory() as setup:
        await CustomerRepository.create(setup, cid)
        await BalanceRepository.credit(
            setup, cid, Money(amount=Decimal("100"), currency=Currency.USD)
        )
        await setup.commit()

    async def credit() -> None:
        async with session_factory() as s:
            await BalanceRepository.credit(
                s, cid, Money(amount=Decimal("30"), currency=Currency.USD)
            )
            await asyncio.sleep(0.1)
            await s.commit()

    async def debit() -> None:
        await asyncio.sleep(0.05)
        async with session_factory() as s:
            await BalanceRepository.debit(
                s, cid, Money(amount=Decimal("20"), currency=Currency.USD)
            )
            await s.commit()

    await asyncio.gather(credit(), debit())

    async with session_factory() as final:
        bals = await BalanceRepository.get_all(final, cid)

    assert len(bals) == 1
    assert bals[0].amount == Decimal("110")
