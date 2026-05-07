"""Pricing service tests -- direct, inverse, cross routing, spread.

All math runs against the SQLite-tier rates table; the pricing
function is pure logic plus a single read per leg, so SQLite is
sufficient (no FOR UPDATE involvement). Decimal-only end to end.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from alembic.config import Config as AlembicConfig
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from alembic import command
from app.domain.currency import Currency
from app.domain.quote import Routing
from app.infra.repositories import RateRepository
from app.services import (
    PriceQuote,
    QuoteSourceUnavailable,
    SameCurrencyError,
    quote_pricing,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config(async_url: str) -> AlembicConfig:
    config = AlembicConfig(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", async_url)
    return config


@pytest.fixture(scope="module")
def sqlite_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    db_path = tmp_path_factory.mktemp("pricing") / "test.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    command.upgrade(_alembic_config(async_url), "head")
    yield async_url


@pytest_asyncio.fixture
async def session(sqlite_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(sqlite_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as s:
            yield s
    finally:
        async with factory() as cleanup:
            await cleanup.execute(text("DELETE FROM rates"))
            await cleanup.commit()
        await engine.dispose()


SPREAD = Decimal("0.005")  # 0.5%
NOW = datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC)


async def _seed(
    session: AsyncSession,
    pairs: list[tuple[Currency, Currency, Decimal]],
    fetched_at: datetime = NOW,
) -> None:
    for base, quote, mid in pairs:
        await RateRepository.upsert(
            session,
            base=base,
            quote=quote,
            mid_rate=mid,
            fetched_at=fetched_at,
            source="test",
        )
    await session.commit()


# --- direct & inverse -------------------------------------------------------


async def test_direct_pair_applies_spread(session: AsyncSession) -> None:
    # 1 USD = 130 KES; selling 100 USD with 0.5% spread.
    await _seed(session, [(Currency.USD, Currency.KES, Decimal("130"))])

    q = await quote_pricing(
        session,
        from_currency=Currency.USD,
        to_currency=Currency.KES,
        from_amount=Decimal("100"),
        spread=SPREAD,
        now=NOW,
    )
    assert q.routing == Routing.DIRECT
    assert q.rate_applied == Decimal("130") * (Decimal(1) - SPREAD)
    assert q.to_amount == Decimal("100") * q.rate_applied
    assert isinstance(q, PriceQuote)


async def test_inverse_direct_pair_uses_one_over_mid(session: AsyncSession) -> None:
    # No KES/USD row; only USD/KES (130 KES per USD). Selling 1300 KES.
    await _seed(session, [(Currency.USD, Currency.KES, Decimal("130"))])

    q = await quote_pricing(
        session,
        from_currency=Currency.KES,
        to_currency=Currency.USD,
        from_amount=Decimal("1300"),
        spread=SPREAD,
        now=NOW,
    )
    assert q.routing == Routing.DIRECT
    expected_rate = (Decimal(1) / Decimal("130")) * (Decimal(1) - SPREAD)
    assert q.rate_applied == expected_rate
    assert q.to_amount == Decimal("1300") * expected_rate


async def test_same_currency_rejected(session: AsyncSession) -> None:
    with pytest.raises(SameCurrencyError):
        await quote_pricing(
            session,
            from_currency=Currency.USD,
            to_currency=Currency.USD,
            from_amount=Decimal("10"),
            spread=SPREAD,
            now=NOW,
        )


async def test_non_positive_amount_rejected(session: AsyncSession) -> None:
    await _seed(session, [(Currency.USD, Currency.KES, Decimal("130"))])
    with pytest.raises(ValueError, match="positive"):
        await quote_pricing(
            session,
            from_currency=Currency.USD,
            to_currency=Currency.KES,
            from_amount=Decimal("0"),
            spread=SPREAD,
            now=NOW,
        )


async def test_invalid_spread_rejected(session: AsyncSession) -> None:
    await _seed(session, [(Currency.USD, Currency.KES, Decimal("130"))])
    for bad_spread in (Decimal("-0.01"), Decimal("1"), Decimal("1.5")):
        with pytest.raises(ValueError, match="spread"):
            await quote_pricing(
                session,
                from_currency=Currency.USD,
                to_currency=Currency.KES,
                from_amount=Decimal("10"),
                spread=bad_spread,
                now=NOW,
            )


# --- staleness --------------------------------------------------------------


async def test_direct_stale_unusable_raises(session: AsyncSession) -> None:
    # Seed at NOW - 11 minutes (past the 10-minute threshold).
    fetched_at = NOW - timedelta(minutes=11)
    await _seed(session, [(Currency.USD, Currency.KES, Decimal("130"))], fetched_at=fetched_at)

    with pytest.raises(QuoteSourceUnavailable):
        await quote_pricing(
            session,
            from_currency=Currency.USD,
            to_currency=Currency.KES,
            from_amount=Decimal("100"),
            spread=SPREAD,
            now=NOW,
        )


# --- cross routing ----------------------------------------------------------


async def test_cross_pair_routes_via_usd_when_both_legs_fresh(
    session: AsyncSession,
) -> None:
    await _seed(
        session,
        [
            (Currency.USD, Currency.KES, Decimal("130")),
            (Currency.USD, Currency.NGN, Decimal("1500")),
            # EUR legs fresh too -- USD takes precedence.
            (Currency.EUR, Currency.KES, Decimal("140")),
            (Currency.EUR, Currency.NGN, Decimal("1600")),
        ],
    )

    q = await quote_pricing(
        session,
        from_currency=Currency.KES,
        to_currency=Currency.NGN,
        from_amount=Decimal("100"),
        spread=SPREAD,
        now=NOW,
    )
    assert q.routing == Routing.VIA_USD
    leg1 = (Decimal(1) / Decimal("130")) * (Decimal(1) - SPREAD)  # KES -> USD
    leg2 = Decimal("1500") * (Decimal(1) - SPREAD)  # USD -> NGN
    expected_rate = leg1 * leg2
    assert q.rate_applied == expected_rate
    assert q.to_amount == Decimal("100") * expected_rate


async def test_cross_pair_falls_back_to_eur_when_usd_leg_stale(
    session: AsyncSession,
) -> None:
    # USD/NGN is stale_unusable; EUR legs fresh -- pricing must use EUR.
    await _seed(
        session,
        [(Currency.USD, Currency.KES, Decimal("130"))],
    )
    await _seed(
        session,
        [(Currency.USD, Currency.NGN, Decimal("1500"))],
        fetched_at=NOW - timedelta(minutes=11),
    )
    await _seed(
        session,
        [
            (Currency.EUR, Currency.KES, Decimal("140")),
            (Currency.EUR, Currency.NGN, Decimal("1600")),
        ],
    )

    q = await quote_pricing(
        session,
        from_currency=Currency.KES,
        to_currency=Currency.NGN,
        from_amount=Decimal("100"),
        spread=SPREAD,
        now=NOW,
    )
    assert q.routing == Routing.VIA_EUR


async def test_cross_pair_503_when_both_routes_stale(session: AsyncSession) -> None:
    stale = NOW - timedelta(minutes=11)
    await _seed(
        session,
        [
            (Currency.USD, Currency.KES, Decimal("130")),
            (Currency.USD, Currency.NGN, Decimal("1500")),
            (Currency.EUR, Currency.KES, Decimal("140")),
            (Currency.EUR, Currency.NGN, Decimal("1600")),
        ],
        fetched_at=stale,
    )

    with pytest.raises(QuoteSourceUnavailable):
        await quote_pricing(
            session,
            from_currency=Currency.KES,
            to_currency=Currency.NGN,
            from_amount=Decimal("100"),
            spread=SPREAD,
            now=NOW,
        )


async def test_cross_pair_compounds_spread(session: AsyncSession) -> None:
    # Mathematical check: total spread after compounding.
    await _seed(
        session,
        [
            (Currency.USD, Currency.KES, Decimal("130")),
            (Currency.USD, Currency.NGN, Decimal("1500")),
        ],
    )
    q = await quote_pricing(
        session,
        from_currency=Currency.KES,
        to_currency=Currency.NGN,
        from_amount=Decimal("130"),  # one USD's worth of KES
        spread=SPREAD,
        now=NOW,
    )
    # mid-rate cross would be 1500 / 130 = 11.538461... NGN per KES.
    # Effective rate compounds the spread -> mid_cross * (1 - s)^2.
    mid_cross = Decimal("1500") / Decimal("130")
    expected_rate = mid_cross * (Decimal(1) - SPREAD) * (Decimal(1) - SPREAD)
    assert q.rate_applied == expected_rate


# --- decimal-only contract --------------------------------------------------


async def test_no_floats_anywhere_in_result(session: AsyncSession) -> None:
    await _seed(session, [(Currency.USD, Currency.KES, Decimal("130"))])
    q = await quote_pricing(
        session,
        from_currency=Currency.USD,
        to_currency=Currency.KES,
        from_amount=Decimal("100"),
        spread=SPREAD,
        now=NOW,
    )
    for value in (q.rate_applied, q.to_amount, q.from_amount):
        assert isinstance(value, Decimal)
        assert not isinstance(value, float)
