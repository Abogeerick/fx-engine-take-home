"""Execute orchestrator tests on SQLite.

Covers the SPEC §7 step sequence as observable from the outside:
happy path (201 + balances move), ownership mismatch (404),
already-consumed quote (409), expired quote (410), insufficient
balance (422), idempotent replay (200, byte-identical body), sticky
failure replay, and idempotency reuse with a different quote_id (409).

Postgres-only behaviours -- two parallel executes racing on the
partial unique index, and credit-leg fault injection -- live in
``tests/integration/test_execute_postgres.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic.config import Config as AlembicConfig
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from alembic import command
from app.domain.clock import FrozenClock
from app.domain.currency import Currency
from app.domain.execution import ExecutionStatus, FailureReason
from app.domain.money import Money
from app.domain.quote import QUOTE_TTL, Routing
from app.infra.models import Execution, LedgerEntry, Quote
from app.infra.repositories import (
    BalanceRepository,
    CustomerRepository,
    QuoteRepository,
)
from app.services import ExecuteRequest, execute_quote

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config(async_url: str) -> AlembicConfig:
    config = AlembicConfig(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", async_url)
    return config


@pytest.fixture(scope="module")
def sqlite_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    db_path = tmp_path_factory.mktemp("execute") / "test.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    command.upgrade(_alembic_config(async_url), "head")
    yield async_url


@pytest_asyncio.fixture
async def session_factory(
    sqlite_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(sqlite_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        async with factory() as cleanup:
            await cleanup.execute(text("DELETE FROM ledger_entries"))
            await cleanup.execute(text("DELETE FROM executions"))
            await cleanup.execute(text("DELETE FROM quotes"))
            await cleanup.execute(text("DELETE FROM balances"))
            await cleanup.execute(text("DELETE FROM customers"))
            await cleanup.commit()
        await engine.dispose()


@pytest_asyncio.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        yield s


# --- helpers ---------------------------------------------------------------


NOW = datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC)


async def _setup_customer_with_usd(session: AsyncSession, *, usd_balance: Decimal) -> UUID:
    cid = uuid4()
    await CustomerRepository.create(session, cid)
    await BalanceRepository.credit(session, cid, Money(amount=usd_balance, currency=Currency.USD))
    return cid


async def _create_quote(
    session: AsyncSession,
    *,
    customer_id,
    from_currency: Currency = Currency.USD,
    to_currency: Currency = Currency.KES,
    from_amount: Decimal = Decimal("100"),
    to_amount: Decimal = Decimal("12967.50"),
    rate: Decimal = Decimal("129.675"),
    routing: Routing = Routing.DIRECT,
    now: datetime = NOW,
) -> Quote:
    return await QuoteRepository.create(
        session,
        quote_id=uuid4(),
        customer_id=customer_id,
        from_currency=from_currency,
        to_currency=to_currency,
        from_amount=from_amount,
        to_amount=to_amount,
        rate_applied=rate,
        routing=routing,
        now=now,
    )


# --- happy path ------------------------------------------------------------


async def test_first_execute_succeeds_and_persists(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    clock = FrozenClock(start=NOW)
    async with session_factory() as setup:
        cid = await _setup_customer_with_usd(setup, usd_balance=Decimal("1000"))
        quote = await _create_quote(setup, customer_id=cid)
        await setup.commit()
        qid = quote.id

    async with session_factory() as session:
        async with session.begin():
            outcome = await execute_quote(
                session,
                ExecuteRequest(quote_id=qid, customer_id=cid, idempotency_key="key-1"),
                clock,
            )

    assert outcome.http_status == 201
    assert outcome.is_replay is False
    assert outcome.response_body["status"] == "succeeded"
    assert outcome.response_body["debited"] == {"currency": "USD", "amount": "100.00"}
    assert outcome.response_body["credited"] == {
        "currency": "KES",
        "amount": "12967.50",
    }
    assert outcome.response_body["balances_after"] == {
        "USD": "900.00",
        "KES": "12967.50",
    }

    async with session_factory() as verify:
        bals = await BalanceRepository.get_all(verify, cid)
        by_currency = {b.currency: b.amount for b in bals}
        assert by_currency["USD"] == Decimal("900")
        assert by_currency["KES"] == Decimal("12967.50")

        # Ledger has exactly two entries.
        rows = (await verify.execute(select(LedgerEntry))).scalars().all()
        assert len(rows) == 2
        amounts_by_currency = {r.currency: r.amount for r in rows}
        assert amounts_by_currency["USD"] == Decimal("-100")
        assert amounts_by_currency["KES"] == Decimal("12967.50")

        # Quote marked consumed.
        q = await verify.get(Quote, qid)
        assert q is not None
        assert q.consumed_at is not None
        assert q.consumed_by_execution_id is not None

        # Execution row succeeded.
        execs = (await verify.execute(select(Execution))).scalars().all()
        assert len(execs) == 1
        assert execs[0].status == ExecutionStatus.SUCCEEDED.value


# --- idempotent replay -----------------------------------------------------


async def test_idempotent_replay_returns_byte_identical_body(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    clock = FrozenClock(start=NOW)
    async with session_factory() as setup:
        cid = await _setup_customer_with_usd(setup, usd_balance=Decimal("1000"))
        quote = await _create_quote(setup, customer_id=cid)
        await setup.commit()
        qid = quote.id

    request = ExecuteRequest(quote_id=qid, customer_id=cid, idempotency_key="dupe-key")

    async with session_factory() as session:
        async with session.begin():
            first = await execute_quote(session, request, clock)

    async with session_factory() as session:
        async with session.begin():
            second = await execute_quote(session, request, clock)

    assert first.http_status == 201
    assert second.http_status == 200
    assert second.is_replay is True
    assert first.response_body == second.response_body  # byte-identical

    # Balance changed exactly once (M=1, not 2).
    async with session_factory() as verify:
        bals = await BalanceRepository.get_all(verify, cid)
        by_currency = {b.currency: b.amount for b in bals}
        assert by_currency["USD"] == Decimal("900")  # not 800
        assert by_currency["KES"] == Decimal("12967.50")  # not 25935


async def test_idempotent_replay_repeated_M_times(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """SPEC §12 graded test: M=10 retries -> 1 success + 9 byte-identical replays,
    balance changes once."""
    clock = FrozenClock(start=NOW)
    async with session_factory() as setup:
        cid = await _setup_customer_with_usd(setup, usd_balance=Decimal("1000"))
        quote = await _create_quote(setup, customer_id=cid)
        await setup.commit()
        qid = quote.id

    request = ExecuteRequest(quote_id=qid, customer_id=cid, idempotency_key="m10")

    bodies: list[dict] = []
    statuses: list[int] = []
    for _ in range(10):
        async with session_factory() as s:
            async with s.begin():
                outcome = await execute_quote(s, request, clock)
        bodies.append(outcome.response_body)
        statuses.append(outcome.http_status)

    assert statuses[0] == 201
    assert all(c == 200 for c in statuses[1:])
    # All bodies equal the first.
    assert all(body == bodies[0] for body in bodies[1:])

    async with session_factory() as verify:
        bals = await BalanceRepository.get_all(verify, cid)
        by_currency = {b.currency: b.amount for b in bals}
        assert by_currency["USD"] == Decimal("900")


# --- sticky failures -------------------------------------------------------


async def test_insufficient_balance_returns_422_and_sticky(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    clock = FrozenClock(start=NOW)
    async with session_factory() as setup:
        cid = await _setup_customer_with_usd(setup, usd_balance=Decimal("50"))
        quote = await _create_quote(setup, customer_id=cid)  # needs 100 USD
        await setup.commit()
        qid = quote.id

    request = ExecuteRequest(quote_id=qid, customer_id=cid, idempotency_key="poor")

    async with session_factory() as s:
        async with s.begin():
            first = await execute_quote(s, request, clock)
    assert first.http_status == 422
    assert first.response_body["status"] == "failed"
    assert first.response_body["failure_reason"] == FailureReason.INSUFFICIENT_BALANCE.value

    # Quote NOT consumed.
    async with session_factory() as verify:
        q = await verify.get(Quote, qid)
        assert q is not None
        assert q.consumed_at is None
        # Balance unchanged.
        bals = await BalanceRepository.get_all(verify, cid)
        assert {b.currency: b.amount for b in bals}["USD"] == Decimal("50")

    # Sticky: retry with same key returns the same 422 from response_body
    # via the replay path (HTTP 200 wrapping the failure body).
    async with session_factory() as s:
        async with s.begin():
            replay = await execute_quote(s, request, clock)
    assert replay.http_status == 200
    assert replay.is_replay is True
    assert replay.response_body == first.response_body


async def test_quote_ownership_mismatch_returns_404(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    clock = FrozenClock(start=NOW)
    async with session_factory() as setup:
        cid_a = await _setup_customer_with_usd(setup, usd_balance=Decimal("1000"))
        cid_b = await _setup_customer_with_usd(setup, usd_balance=Decimal("1000"))
        quote = await _create_quote(setup, customer_id=cid_a)
        await setup.commit()
        qid = quote.id

    async with session_factory() as s:
        async with s.begin():
            outcome = await execute_quote(
                s,
                ExecuteRequest(quote_id=qid, customer_id=cid_b, idempotency_key="b-1"),
                clock,
            )

    assert outcome.http_status == 404
    assert outcome.response_body["failure_reason"] == (FailureReason.QUOTE_OWNERSHIP_MISMATCH.value)

    # Quote NOT consumed.
    async with session_factory() as verify:
        q = await verify.get(Quote, qid)
        assert q is not None
        assert q.consumed_at is None


async def test_expired_quote_returns_410(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    clock = FrozenClock(start=NOW)
    async with session_factory() as setup:
        cid = await _setup_customer_with_usd(setup, usd_balance=Decimal("1000"))
        quote = await _create_quote(setup, customer_id=cid, now=NOW)
        await setup.commit()
        qid = quote.id

    # Advance past the TTL.
    clock.tick(QUOTE_TTL + timedelta(seconds=1))

    async with session_factory() as s:
        async with s.begin():
            outcome = await execute_quote(
                s,
                ExecuteRequest(quote_id=qid, customer_id=cid, idempotency_key="late"),
                clock,
            )

    assert outcome.http_status == 410
    assert outcome.response_body["failure_reason"] == FailureReason.QUOTE_EXPIRED.value


# --- idempotency-key reuse with different quote ----------------------------


async def test_idempotency_reuse_with_different_quote_returns_409(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    clock = FrozenClock(start=NOW)
    async with session_factory() as setup:
        cid = await _setup_customer_with_usd(setup, usd_balance=Decimal("1000"))
        first_quote = await _create_quote(setup, customer_id=cid)
        second_quote = await _create_quote(
            setup,
            customer_id=cid,
            from_amount=Decimal("50"),
            to_amount=Decimal("6483.75"),
        )
        await setup.commit()
        qid_a = first_quote.id
        qid_b = second_quote.id

    async with session_factory() as s:
        async with s.begin():
            first = await execute_quote(
                s,
                ExecuteRequest(quote_id=qid_a, customer_id=cid, idempotency_key="reused"),
                clock,
            )
    assert first.http_status == 201

    async with session_factory() as s:
        async with s.begin():
            second = await execute_quote(
                s,
                ExecuteRequest(quote_id=qid_b, customer_id=cid, idempotency_key="reused"),
                clock,
            )
    assert second.http_status == 409
    assert second.response_body["failure_reason"] == (FailureReason.IDEMPOTENCY_KEY_REUSED.value)

    # Second quote NOT consumed (no execution row inserted for it).
    async with session_factory() as verify:
        q = await verify.get(Quote, qid_b)
        assert q is not None
        assert q.consumed_at is None
        # Only one execution row total.
        rows = (await verify.execute(select(Execution))).scalars().all()
        assert len(rows) == 1


# --- already-consumed quote (different idempotency key) --------------------


async def test_already_consumed_quote_returns_409(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    clock = FrozenClock(start=NOW)
    async with session_factory() as setup:
        cid = await _setup_customer_with_usd(setup, usd_balance=Decimal("1000"))
        quote = await _create_quote(setup, customer_id=cid)
        await setup.commit()
        qid = quote.id

    async with session_factory() as s:
        async with s.begin():
            first = await execute_quote(
                s,
                ExecuteRequest(quote_id=qid, customer_id=cid, idempotency_key="k1"),
                clock,
            )
    assert first.http_status == 201

    async with session_factory() as s:
        async with s.begin():
            second = await execute_quote(
                s,
                ExecuteRequest(quote_id=qid, customer_id=cid, idempotency_key="k2"),
                clock,
            )
    assert second.http_status == 409
    assert second.response_body["failure_reason"] == (FailureReason.QUOTE_ALREADY_CONSUMED.value)
