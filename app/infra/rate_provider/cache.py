"""Rate cache -- write-through to the ``rates`` table.

The cache layer owns its own session_factory because the rates table
is the rate provider's private store. Quote and execute requests do
not pass a session in to read rates; they ask the ``RateProvider``,
which goes through this cache.

Reads return a ``CacheEntry`` annotated with the staleness tier
classified at the caller's ``now``. The caller decides whether the
tier is acceptable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.currency import Currency
from app.domain.staleness import StalenessTier, classify
from app.infra.repositories import RateRepository


@dataclass(frozen=True)
class CacheEntry:
    mid_rate: Decimal
    fetched_at: datetime
    tier: StalenessTier


class RateCache:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(
        self,
        *,
        base: Currency,
        quote: Currency,
        now: datetime,
    ) -> CacheEntry | None:
        async with self._session_factory() as session:
            row = await RateRepository.get(session, base=base, quote=quote)
        if row is None:
            return None
        mid_rate, fetched_at = row
        return CacheEntry(
            mid_rate=mid_rate,
            fetched_at=fetched_at,
            tier=classify(fetched_at=fetched_at, now=now),
        )

    async def upsert(
        self,
        *,
        base: Currency,
        quote: Currency,
        mid_rate: Decimal,
        fetched_at: datetime,
        source: str,
    ) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await RateRepository.upsert(
                    session,
                    base=base,
                    quote=quote,
                    mid_rate=mid_rate,
                    fetched_at=fetched_at,
                    source=source,
                )
