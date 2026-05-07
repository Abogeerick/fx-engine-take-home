"""Customer repository -- create/get only.

The fixture endpoint ``POST /customers`` calls ``create``; production
callers come from upstream services that already have a customer_id
and use ``get`` here.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.models import Customer


class CustomerRepository:
    @staticmethod
    async def create(session: AsyncSession, customer_id: UUID) -> Customer:
        c = Customer(id=customer_id)
        session.add(c)
        await session.flush()
        return c

    @staticmethod
    async def get(session: AsyncSession, customer_id: UUID) -> Customer | None:
        return await session.get(Customer, customer_id)
