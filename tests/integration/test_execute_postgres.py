"""Postgres-only execute orchestrator tests.

Two scenarios SQLite cannot test honestly:

1. **Two parallel executes on the same quote with different keys.**
   Both insert their pending execution rows in their own transactions.
   ``SELECT ... FOR UPDATE`` on the quote serialises them: the winner
   marks the quote consumed and commits with status='succeeded'; the
   loser, on unblocking, sees ``consumed_at IS NOT NULL`` and returns
   409. Exactly one execution row ends up succeeded; the partial
   unique index is the defence-in-depth backstop.

2. **Credit-leg fault injection.** A monkey-patched ``credit`` raises
   after the debit has happened. The outer transaction rolls back,
   leaving the balance unchanged and no committed execution row.
   This validates the atomicity claim.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.clock import FrozenClock
from app.domain.currency import Currency
from app.domain.money import Money
from app.domain.quote import Routing
from app.infra.models import Execution, Quote
from app.infra.repositories import (
    BalanceRepository,
    CustomerRepository,
    QuoteRepository,
)
from app.services import ExecuteRequest, execute_quote

NOW = datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC)


async def _setup_customer_with_quote(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple:
    cid = uuid4()
    async with session_factory() as setup:
        await CustomerRepository.create(setup, cid)
        await BalanceRepository.credit(
            setup, cid, Money(amount=Decimal("1000"), currency=Currency.USD)
        )
        quote = await QuoteRepository.create(
            setup,
            quote_id=uuid4(),
            customer_id=cid,
            from_currency=Currency.USD,
            to_currency=Currency.KES,
            from_amount=Decimal("100"),
            to_amount=Decimal("12967.50"),
            rate_applied=Decimal("129.675"),
            routing=Routing.DIRECT,
            now=NOW,
        )
        await setup.commit()
        qid = quote.id
    return cid, qid


async def test_two_parallel_executes_on_same_quote_serialise(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    cid, qid = await _setup_customer_with_quote(session_factory)
    clock = FrozenClock(start=NOW)

    async def attempt(key: str) -> int:
        async with session_factory() as session:
            async with session.begin():
                outcome = await execute_quote(
                    session,
                    ExecuteRequest(quote_id=qid, customer_id=cid, idempotency_key=key),
                    clock,
                )
        return outcome.http_status

    statuses = await asyncio.gather(attempt("k-a"), attempt("k-b"))

    # Exactly one 201, exactly one 409. Order is non-deterministic.
    assert sorted(statuses) == [201, 409]

    # Final balance reflects exactly one execution.
    async with session_factory() as verify:
        bals = await BalanceRepository.get_all(verify, cid)
        by_currency = {b.currency: b.amount for b in bals}
        assert by_currency["USD"] == Decimal("900")
        assert by_currency["KES"] == Decimal("12967.50")

        # Exactly one succeeded execution; the loser is status='failed'.
        rows = (await verify.execute(select(Execution))).scalars().all()
        statuses_by_row = sorted(r.status for r in rows)
        assert statuses_by_row == ["failed", "succeeded"]


async def test_atomicity_credit_leg_failure_rolls_back_debit(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inject a fault in BalanceRepository.credit so it raises after the
    debit has flushed. The outer transaction must roll back the debit
    and the (placeholder) execution row -- balance unchanged, no
    committed execution."""
    cid, qid = await _setup_customer_with_quote(session_factory)
    clock = FrozenClock(start=NOW)

    original_credit = BalanceRepository.credit

    async def faulty_credit(session: AsyncSession, customer_id, money: Money):  # type: ignore[no-untyped-def]
        if money.currency == Currency.KES:
            raise RuntimeError("simulated credit-leg failure")
        return await original_credit(session, customer_id, money)

    monkeypatch.setattr(BalanceRepository, "credit", faulty_credit)

    with pytest.raises(RuntimeError, match="simulated credit-leg failure"):
        async with session_factory() as session:
            async with session.begin():
                await execute_quote(
                    session,
                    ExecuteRequest(quote_id=qid, customer_id=cid, idempotency_key="atomicity"),
                    clock,
                )

    # Rollback verification: balance unchanged, quote unconsumed,
    # no committed execution row.
    async with session_factory() as verify:
        bals = await BalanceRepository.get_all(verify, cid)
        by_currency = {b.currency: b.amount for b in bals}
        assert by_currency["USD"] == Decimal("1000")  # debit was rolled back
        assert by_currency.get("KES", Decimal("0")) == Decimal("0")

        q = await verify.get(Quote, qid)
        assert q is not None
        assert q.consumed_at is None

        rows = (await verify.execute(select(Execution))).scalars().all()
        # The placeholder execution row was inside the rolled-back tx.
        assert len(rows) == 0

    # The idempotency key is now free for retry -- caller can submit
    # a new request with the same key without sticky-failure. Undo the
    # fault before retrying.
    monkeypatch.undo()
    async with session_factory() as session:
        async with session.begin():
            outcome = await execute_quote(
                session,
                ExecuteRequest(quote_id=qid, customer_id=cid, idempotency_key="atomicity"),
                clock,
            )
    assert outcome.http_status == 201
