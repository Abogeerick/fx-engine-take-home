"""Ledger repository -- append-only signed entries.

Each successful execution writes exactly two ledger entries: a
debit (negative amount) on the from-currency and a credit (positive
amount) on the to-currency. Balances are derived from the ledger but
materialised in the ``balances`` table for fast reads (per SPEC §4);
this repository writes only the ledger side.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.currency import Currency
from app.infra.models import LedgerEntry


class LedgerRepository:
    @staticmethod
    async def record_pair(
        session: AsyncSession,
        *,
        execution_id: UUID,
        customer_id: UUID,
        debit_currency: Currency,
        debit_amount: Decimal,
        credit_currency: Currency,
        credit_amount: Decimal,
        now: datetime,
    ) -> tuple[LedgerEntry, LedgerEntry]:
        """Write the (debit, credit) pair for an execution.

        ``debit_amount`` and ``credit_amount`` are both supplied as
        positive Decimals; the repository writes them with the
        appropriate sign so callers don't have to remember the
        convention.
        """
        if now.tzinfo is None:
            raise ValueError("now must be tz-aware")
        if debit_amount <= 0 or credit_amount <= 0:
            raise ValueError("debit/credit amounts must be positive Decimals")

        debit_entry = LedgerEntry(
            id=uuid4(),
            execution_id=execution_id,
            customer_id=customer_id,
            currency=debit_currency.value,
            amount=-debit_amount,
            created_at=now,
        )
        credit_entry = LedgerEntry(
            id=uuid4(),
            execution_id=execution_id,
            customer_id=customer_id,
            currency=credit_currency.value,
            amount=credit_amount,
            created_at=now,
        )
        session.add_all([debit_entry, credit_entry])
        await session.flush()
        return (debit_entry, credit_entry)
