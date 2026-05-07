"""Balance repository -- read-for-update + signed mutations.

``get_for_update`` is the primary read path used by the execute
transaction (step 3). On Postgres it emits ``SELECT ... FOR UPDATE``,
acquiring a row-level lock until the transaction commits. On SQLite
the ``with_for_update()`` clause is silently dropped by the dialect;
correctness for SQLite-tier tests is preserved by SQLite's
database-level write serialization.

``credit`` and ``debit`` operate on a ``Money`` value (which already
carries its currency). They flush so subsequent reads in the same
session see the change but do not commit -- the caller controls
transaction boundaries.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.currency import Currency
from app.domain.money import Money
from app.infra.models import Balance


class InsufficientBalance(Exception):
    """Raised by ``debit`` when the available balance is below the requested amount."""


class BalanceRepository:
    @staticmethod
    async def get_for_update(
        session: AsyncSession,
        customer_id: UUID,
        currency: Currency,
    ) -> Balance | None:
        stmt = (
            select(Balance)
            .where(
                Balance.customer_id == customer_id,
                Balance.currency == currency.value,
            )
            .with_for_update()
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    async def credit(
        session: AsyncSession,
        customer_id: UUID,
        money: Money,
    ) -> Balance:
        if money.amount <= 0:
            raise ValueError(f"credit amount must be positive; got {money.amount}")
        row = await BalanceRepository._get_or_create(session, customer_id, money.currency)
        row.amount = row.amount + money.amount
        await session.flush()
        return row

    @staticmethod
    async def debit(
        session: AsyncSession,
        customer_id: UUID,
        money: Money,
    ) -> Balance:
        if money.amount <= 0:
            raise ValueError(f"debit amount must be positive; got {money.amount}")
        row = await BalanceRepository._get_or_create(session, customer_id, money.currency)
        if row.amount < money.amount:
            raise InsufficientBalance(
                f"insufficient {money.currency.value}: have {row.amount}, need {money.amount}"
            )
        row.amount = row.amount - money.amount
        await session.flush()
        return row

    @staticmethod
    async def get_all(
        session: AsyncSession,
        customer_id: UUID,
    ) -> Sequence[Balance]:
        stmt = select(Balance).where(Balance.customer_id == customer_id)
        result = await session.execute(stmt)
        return result.scalars().all()

    @staticmethod
    async def _get_or_create(
        session: AsyncSession,
        customer_id: UUID,
        currency: Currency,
    ) -> Balance:
        existing = await BalanceRepository.get_for_update(session, customer_id, currency)
        if existing is not None:
            return existing
        row = Balance(
            customer_id=customer_id,
            currency=currency.value,
            amount=Decimal("0"),
        )
        session.add(row)
        await session.flush()
        return row
