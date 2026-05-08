"""Hypothesis property tests for Decimal precision invariants.

SPEC §12 #1: ``for random valid amounts and random pairs, the round-
trip quote -> execute -> balance invariant holds: post-balances
reflect the rounded from_amount and to_amount exactly; no fractional
drift.``

The property runs against the orchestrator directly (no HTTP) on a
fresh in-memory SQLite per example, schema established via SQLAlchemy
metadata for speed (the migration is exercised separately in step 2's
roundtrip test). Each example is fully isolated.

The test deliberately uses ``Currency.NGN`` and other 2-minor-unit
currencies; widening to per-currency minor-unit variation is a
straight extension once the underlying invariant holds.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.domain.clock import FrozenClock
from app.domain.currency import Currency
from app.domain.money import Money
from app.infra.models import Base
from app.infra.repositories import (
    BalanceRepository,
    CustomerRepository,
    QuoteRepository,
    RateRepository,
)
from app.services import ExecuteRequest, execute_quote, quote_pricing

NOW = datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC)
SPREAD = Decimal("0.005")

# Mid-rates seeded per example. Chosen to span the realistic spectrum
# (USD<->KES is ~130; USD<->NGN is ~1500; USD<->EUR is ~0.92).
_MIDS: dict[tuple[Currency, Currency], Decimal] = {
    (Currency.USD, Currency.KES): Decimal("130"),
    (Currency.USD, Currency.NGN): Decimal("1500"),
    (Currency.USD, Currency.EUR): Decimal("0.92"),
    (Currency.EUR, Currency.KES): Decimal("141.30"),
    (Currency.EUR, Currency.NGN): Decimal("1630.43"),
}

# All ten direct + inverse-direct pairs plus the two crosses.
_PAIRS: list[tuple[Currency, Currency]] = [
    (Currency.USD, Currency.KES),
    (Currency.USD, Currency.NGN),
    (Currency.USD, Currency.EUR),
    (Currency.EUR, Currency.KES),
    (Currency.EUR, Currency.NGN),
    (Currency.KES, Currency.USD),
    (Currency.NGN, Currency.USD),
    (Currency.EUR, Currency.USD),
    (Currency.KES, Currency.EUR),
    (Currency.NGN, Currency.EUR),
    (Currency.KES, Currency.NGN),
    (Currency.NGN, Currency.KES),
]


async def _seed_rates(session_factory) -> None:
    async with session_factory() as s:
        async with s.begin():
            for (base, quote), mid in _MIDS.items():
                await RateRepository.upsert(
                    s,
                    base=base,
                    quote=quote,
                    mid_rate=mid,
                    fetched_at=NOW,
                    source="hypothesis",
                )


async def _per_example_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory


async def _scenario(from_amount: Decimal, pair_idx: int) -> None:
    from_curr, to_curr = _PAIRS[pair_idx]

    engine, factory = await _per_example_engine()
    try:
        await _seed_rates(factory)

        cid = uuid4()
        # Fund the customer with comfortable headroom over the trade size.
        funding = (from_amount * Decimal("10")).quantize(Decimal("0.01"))
        async with factory() as s:
            async with s.begin():
                await CustomerRepository.create(s, cid)
                await BalanceRepository.credit(s, cid, Money(amount=funding, currency=from_curr))

        # Price the trade.
        async with factory() as s:
            price = await quote_pricing(
                s,
                from_currency=from_curr,
                to_currency=to_curr,
                from_amount=from_amount,
                spread=SPREAD,
                now=NOW,
            )

        # Quote what we book (SPEC §3): persist the rounded amounts.
        from_amount_q = (
            Money(amount=from_amount, currency=from_curr).quantize_to_minor_units().amount
        )
        to_amount_q = (
            Money(amount=price.to_amount, currency=to_curr).quantize_to_minor_units().amount
        )

        # Skip examples that round to zero on either leg (degenerate trade
        # below the smallest bookable unit).
        if from_amount_q <= 0 or to_amount_q <= 0:
            return
        # Skip examples where the funding doesn't cover the rounded debit.
        if funding < from_amount_q:
            return

        quote_id = uuid4()
        async with factory() as s:
            async with s.begin():
                await QuoteRepository.create(
                    s,
                    quote_id=quote_id,
                    customer_id=cid,
                    from_currency=from_curr,
                    to_currency=to_curr,
                    from_amount=from_amount_q,
                    to_amount=to_amount_q,
                    rate_applied=price.rate_applied,
                    routing=price.routing,
                    now=NOW,
                )

        async with factory() as s:
            pre = {b.currency: b.amount for b in await BalanceRepository.get_all(s, cid)}

        async with factory() as s:
            async with s.begin():
                outcome = await execute_quote(
                    s,
                    ExecuteRequest(
                        quote_id=quote_id,
                        customer_id=cid,
                        idempotency_key=f"hyp-{quote_id}",
                    ),
                    FrozenClock(start=NOW),
                )
        assert outcome.http_status == 201, outcome.response_body

        async with factory() as s:
            post = {b.currency: b.amount for b in await BalanceRepository.get_all(s, cid)}

        # The invariant: from-currency dropped by exactly from_amount_q,
        # to-currency rose by exactly to_amount_q. No fractional drift.
        pre_from = pre.get(from_curr.value, Decimal("0"))
        post_from = post.get(from_curr.value, Decimal("0"))
        pre_to = pre.get(to_curr.value, Decimal("0"))
        post_to = post.get(to_curr.value, Decimal("0"))

        assert post_from == pre_from - from_amount_q, (
            f"from-balance drift: {pre_from} -> {post_from}, expected delta {-from_amount_q}"
        )
        assert post_to == pre_to + to_amount_q, (
            f"to-balance drift: {pre_to} -> {post_to}, expected delta {to_amount_q}"
        )

        # The response_body's reported balances must equal the DB state
        # (no read-after-commit divergence -- verifies the orchestrator's
        # post-flush-read claim).
        body_after = outcome.response_body["balances_after"]
        assert Decimal(body_after[from_curr.value]) == post_from
        assert Decimal(body_after[to_curr.value]) == post_to
    finally:
        await engine.dispose()


@settings(
    deadline=None,
    max_examples=40,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    from_amount=st.decimals(
        min_value=Decimal("0.01"),
        max_value=Decimal("1000"),
        places=2,
        allow_nan=False,
        allow_infinity=False,
    ),
    pair_idx=st.integers(min_value=0, max_value=len(_PAIRS) - 1),
)
def test_round_trip_quote_execute_balance_no_drift(from_amount: Decimal, pair_idx: int) -> None:
    asyncio.run(_scenario(from_amount, pair_idx))
