"""Quote repository -- create, read-for-update, mark consumed."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.currency import Currency
from app.domain.quote import QUOTE_TTL, Routing
from app.infra.models import Quote


class QuoteRepository:
    @staticmethod
    async def create(
        session: AsyncSession,
        *,
        quote_id: UUID,
        customer_id: UUID,
        from_currency: Currency,
        to_currency: Currency,
        from_amount: Decimal,
        to_amount: Decimal,
        rate_applied: Decimal,
        routing: Routing,
        now: datetime,
    ) -> Quote:
        if now.tzinfo is None:
            raise ValueError("now must be tz-aware")
        q = Quote(
            id=quote_id,
            customer_id=customer_id,
            from_currency=from_currency.value,
            to_currency=to_currency.value,
            from_amount=from_amount,
            to_amount=to_amount,
            rate_applied=rate_applied,
            routing=routing.value,
            created_at=now,
            expires_at=now + QUOTE_TTL,
        )
        session.add(q)
        await session.flush()
        return q

    @staticmethod
    async def get_for_update(
        session: AsyncSession,
        quote_id: UUID,
    ) -> Quote | None:
        stmt = select(Quote).where(Quote.id == quote_id).with_for_update()
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    async def mark_consumed(
        session: AsyncSession,
        quote: Quote,
        *,
        execution_id: UUID,
        now: datetime,
    ) -> None:
        if now.tzinfo is None:
            raise ValueError("now must be tz-aware")
        quote.consumed_at = now
        quote.consumed_by_execution_id = execution_id
        await session.flush()
