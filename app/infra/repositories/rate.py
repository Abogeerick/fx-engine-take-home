"""Rate repository -- upsert + read + freshness classification.

The upsert uses dialect-native ``ON CONFLICT DO UPDATE``; both
Postgres and SQLite expose the same surface in SQLAlchemy 2.x so the
branching is on the dialect name only.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.currency import Currency
from app.domain.staleness import StalenessTier, classify
from app.infra.models import Rate


class RateRepository:
    @staticmethod
    async def upsert(
        session: AsyncSession,
        *,
        base: Currency,
        quote: Currency,
        mid_rate: Decimal,
        fetched_at: datetime,
        source: str,
    ) -> None:
        if mid_rate <= 0:
            raise ValueError(f"mid_rate must be positive; got {mid_rate}")
        if fetched_at.tzinfo is None:
            raise ValueError("fetched_at must be tz-aware")

        bind = session.get_bind()
        dialect_name = bind.dialect.name
        if dialect_name == "postgresql":
            insert_fn = pg_insert
        elif dialect_name == "sqlite":
            insert_fn = sqlite_insert
        else:
            raise RuntimeError(f"unsupported dialect for rate upsert: {dialect_name}")

        values = {
            "base_currency": base.value,
            "quote_currency": quote.value,
            "mid_rate": mid_rate,
            "fetched_at": fetched_at,
            "source": source,
        }
        stmt = insert_fn(Rate).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["base_currency", "quote_currency"],
            set_={
                "mid_rate": stmt.excluded.mid_rate,
                "fetched_at": stmt.excluded.fetched_at,
                "source": stmt.excluded.source,
            },
        )
        await session.execute(stmt)

    @staticmethod
    async def get(
        session: AsyncSession,
        *,
        base: Currency,
        quote: Currency,
    ) -> tuple[Decimal, datetime] | None:
        stmt = select(Rate.mid_rate, Rate.fetched_at).where(
            Rate.base_currency == base.value,
            Rate.quote_currency == quote.value,
        )
        result = await session.execute(stmt)
        row = result.one_or_none()
        if row is None:
            return None
        return (row.mid_rate, row.fetched_at)

    @staticmethod
    async def freshness_tier(
        session: AsyncSession,
        *,
        base: Currency,
        quote: Currency,
        now: datetime,
    ) -> StalenessTier | None:
        """Return the tier for the (base, quote) row, or None if absent.

        ``now`` is supplied by the caller (typically a ``Clock``); this
        method does not read the wall clock so tests can advance time
        deterministically.
        """
        rate = await RateRepository.get(session, base=base, quote=quote)
        if rate is None:
            return None
        _, fetched_at = rate
        return classify(fetched_at=fetched_at, now=now)
