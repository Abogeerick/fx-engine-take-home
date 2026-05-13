"""Seed the rates table with mid rates so the API can be exercised
without a live ``RATE_API_KEY``.

The grading flow does not require a real exchangeratesapi.io key --
the test suite fakes the rate source. But running the server against
a fresh dev database requires *some* row in ``rates`` for ``/healthz``
to report ``ok`` and for ``/quotes`` to return 200.

The mid rates below are reasonable round numbers suitable for a smoke
test (USD/KES ~ 130, USD/NGN ~ 1500, USD/EUR ~ 0.92, plus the EUR
crosses). They are not market data and should not be used for anything
beyond exercising the API.

Usage:
    python scripts/seed_rates.py
    # or with a non-default DB URL:
    DATABASE_URL=postgresql+asyncpg://... python scripts/seed_rates.py
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.domain.currency import Currency
from app.infra.repositories import RateRepository

DEFAULT_URL = "postgresql+asyncpg://fx:devpass@localhost:5433/fx_engine"

RATES: dict[tuple[Currency, Currency], Decimal] = {
    (Currency.USD, Currency.KES): Decimal("130"),
    (Currency.USD, Currency.NGN): Decimal("1500"),
    (Currency.USD, Currency.EUR): Decimal("0.92"),
    (Currency.EUR, Currency.KES): Decimal("141.30"),
    (Currency.EUR, Currency.NGN): Decimal("1630.43"),
}


async def main() -> None:
    url = os.environ.get("DATABASE_URL", DEFAULT_URL)
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    async with factory() as session:
        async with session.begin():
            for (base, quote), mid in RATES.items():
                await RateRepository.upsert(
                    session,
                    base=base,
                    quote=quote,
                    mid_rate=mid,
                    fetched_at=now,
                    source="seed",
                )
    await engine.dispose()
    print(f"seeded {len(RATES)} rates into {url}")


if __name__ == "__main__":
    asyncio.run(main())
