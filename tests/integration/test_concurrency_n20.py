"""SPEC §12 #2 graded test: N=20 parallel executes of the same quote.

Twenty parallel calls to the execute orchestrator with distinct
idempotency keys, all targeting the same quote. The expected outcome:

  * exactly 1 call returns 201 (the winner)
  * the other 19 return 409 (quote already consumed)
  * exactly one execution row has status='succeeded'; the other 19
    are status='failed' with failure_reason='quote_already_consumed'
  * the customer balances reflect exactly one execution

This is the load-bearing concurrency test. It exercises the FOR
UPDATE serialisation on the quote row plus the partial unique index
on executions.quote_id WHERE status='succeeded'.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.clock import FrozenClock
from app.domain.currency import Currency
from app.domain.money import Money
from app.domain.quote import Routing
from app.infra.models import Execution
from app.infra.repositories import (
    BalanceRepository,
    CustomerRepository,
    QuoteRepository,
)
from app.services import ExecuteRequest, execute_quote

NOW = datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC)


async def test_n20_parallel_executes_serialise(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
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

    clock = FrozenClock(start=NOW)

    async def attempt(i: int) -> int:
        async with session_factory() as session:
            async with session.begin():
                outcome = await execute_quote(
                    session,
                    ExecuteRequest(
                        quote_id=qid,
                        customer_id=cid,
                        idempotency_key=f"n20-key-{i}",
                    ),
                    clock,
                )
        return outcome.http_status

    statuses = await asyncio.gather(*[attempt(i) for i in range(20)])

    succeeded = [s for s in statuses if s == 201]
    rejected = [s for s in statuses if s == 409]
    assert len(succeeded) == 1, f"expected exactly one 201; got {len(succeeded)}"
    assert len(rejected) == 19, f"expected nineteen 409s; got {len(rejected)}"
    assert len(statuses) == 20

    async with session_factory() as verify:
        bals = {b.currency: b.amount for b in await BalanceRepository.get_all(verify, cid)}
        assert bals["USD"] == Decimal("900"), (
            f"USD balance reflects more than one execution: {bals['USD']}"
        )
        assert bals["KES"] == Decimal("12967.50"), (
            f"KES balance reflects more than one execution: {bals['KES']}"
        )

        # Filter to this quote -- other tests in the same DB may leave
        # execution rows behind via fixtures that don't truncate.
        rows = (
            (await verify.execute(select(Execution).where(Execution.quote_id == qid)))
            .scalars()
            .all()
        )
        assert len(rows) == 20
        succeeded_rows = [r for r in rows if r.status == "succeeded"]
        failed_rows = [r for r in rows if r.status == "failed"]
        assert len(succeeded_rows) == 1
        assert len(failed_rows) == 19
        for r in failed_rows:
            assert r.failure_reason == "quote_already_consumed"
