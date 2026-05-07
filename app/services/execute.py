"""Execute-quote orchestrator -- the atomic two-leg trade.

The transaction lifecycle is owned by the *caller*. The orchestrator
takes an open ``AsyncSession`` and assumes ``async with session.begin():``
wraps the call. Inside, it uses ``begin_nested()`` (SAVEPOINT) for the
idempotency-conflict check so an ``IntegrityError`` on the unique
constraint can be recovered without poisoning the outer transaction.

Step sequence per SPEC §7
=========================

1. (Caller)  begin transaction.
2. Insert into ``executions`` with ``(customer_id, idempotency_key)``
   inside a SAVEPOINT. The unique constraint detects replays:
     - Same key, same quote_id  -> read existing, return its
       response_body with HTTP 200 (replay).
     - Same key, different quote_id -> HTTP 409 (idempotency reuse).
3. ``SELECT ... FOR UPDATE`` on the quote row. Validate, in order:
     a. ``quote.customer_id == request.customer_id``  (else 404)
     b. ``consumed_at IS NULL``                       (else 409)
     c. ``expires_at > now``                          (else 410)
4. ``SELECT ... FOR UPDATE`` on the two balance rows in alphabetical
   currency order to prevent deadlocks under cross-direction swaps.
5. Validate ``from_balance >= from_amount``           (else 422).
6. Update balances: debit from-currency, credit to-currency.
7. Insert two ledger entries (signed: debit negative, credit positive).
8. Update quote: set ``consumed_at``, ``consumed_by_execution_id``.
9. Build ``response_body`` from the post-update balance values
   (``debit/credit`` repository methods return ORM rows whose
   ``amount`` is already the post-flush value -- no separate read,
   no read-after-commit). Persist ``response_body`` and
   ``status='succeeded'`` on the executions row.
10. (Caller) commit.

Outcomes
========

* Success -> HTTP 201, status='succeeded' execution row, response_body
  contains debited / credited / balances_after.
* Business-logic failure (insufficient balance, expired, ownership
  mismatch, already consumed) -> HTTP 410/422/404/409, status='failed'
  execution row with the failure response in response_body. Failures
  are *sticky*: a retry with the same idempotency key returns the
  stored response, not a fresh attempt (SPEC §10).
* DB-level failure (lock timeout, connection drop) -> the orchestrator
  raises; the caller's outer transaction rolls back including the
  executions row, leaving the idempotency key free for retry.
* Idempotency reuse with different quote_id -> HTTP 409, no DB state
  change (the SAVEPOINT rolled back the failed insert).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.clock import Clock
from app.domain.currency import Currency
from app.domain.execution import FailureReason
from app.domain.money import Money
from app.domain.quote import is_expired
from app.infra.models import Execution, Quote
from app.infra.repositories import (
    BalanceRepository,
    ExecutionRepository,
    InsufficientBalance,
    LedgerRepository,
    QuoteRepository,
)


@dataclass(frozen=True)
class ExecuteRequest:
    quote_id: UUID
    customer_id: UUID
    idempotency_key: str


@dataclass(frozen=True)
class ExecuteOutcome:
    response_body: dict[str, Any]
    http_status: int
    is_replay: bool


def _quantize_str(amount: Any, currency: Currency) -> str:
    """Format a Decimal-like amount to the currency's minor units, as a string."""
    return str(Money(amount=amount, currency=currency).quantize_to_minor_units().amount)


async def execute_quote(
    session: AsyncSession,
    request: ExecuteRequest,
    clock: Clock,
) -> ExecuteOutcome:
    if not request.idempotency_key:
        raise ValueError("idempotency_key must be non-empty")

    now = clock.now()
    new_execution_id = uuid4()

    # --- step 2: insert pending execution inside SAVEPOINT --------------
    try:
        async with session.begin_nested():
            new_execution = await ExecutionRepository.insert_pending(
                session,
                execution_id=new_execution_id,
                quote_id=request.quote_id,
                customer_id=request.customer_id,
                idempotency_key=request.idempotency_key,
                now=now,
            )
    except IntegrityError:
        return await _handle_idempotency_conflict(session, request)

    # --- step 3: lock quote and validate --------------------------------
    quote = await QuoteRepository.get_for_update(session, request.quote_id)
    if quote is None:
        # FK enforces this can't happen for the quote_id we just inserted,
        # but be defensive.
        return await _record_failure(
            session,
            new_execution,
            failure_reason=FailureReason.QUOTE_OWNERSHIP_MISMATCH,
            http_status=404,
            message="quote not found",
        )

    if quote.customer_id != request.customer_id:
        return await _record_failure(
            session,
            new_execution,
            failure_reason=FailureReason.QUOTE_OWNERSHIP_MISMATCH,
            http_status=404,
            message="quote not found",
        )

    if quote.consumed_at is not None:
        return await _record_failure(
            session,
            new_execution,
            failure_reason=FailureReason.QUOTE_ALREADY_CONSUMED,
            http_status=409,
            message="quote already consumed by a different request",
        )

    if is_expired(expires_at=quote.expires_at, now=now):
        return await _record_failure(
            session,
            new_execution,
            failure_reason=FailureReason.QUOTE_EXPIRED,
            http_status=410,
            message="quote has expired",
        )

    # --- step 4: lock balances in alphabetical currency order -----------
    from_currency = Currency(quote.from_currency)
    to_currency = Currency(quote.to_currency)
    first, second = sorted([from_currency, to_currency], key=lambda c: c.value)
    # Lock both before any mutation to enforce a global lock order.
    await BalanceRepository.get_for_update(session, request.customer_id, first)
    await BalanceRepository.get_for_update(session, request.customer_id, second)

    # --- steps 5-7: debit, credit, ledger -------------------------------
    from_money = Money(amount=quote.from_amount, currency=from_currency)
    to_money = Money(amount=quote.to_amount, currency=to_currency)

    try:
        debited_row = await BalanceRepository.debit(session, request.customer_id, from_money)
    except InsufficientBalance as exc:
        return await _record_failure(
            session,
            new_execution,
            failure_reason=FailureReason.INSUFFICIENT_BALANCE,
            http_status=422,
            message=str(exc),
        )

    credited_row = await BalanceRepository.credit(session, request.customer_id, to_money)

    await LedgerRepository.record_pair(
        session,
        execution_id=new_execution.id,
        customer_id=request.customer_id,
        debit_currency=from_currency,
        debit_amount=quote.from_amount,
        credit_currency=to_currency,
        credit_amount=quote.to_amount,
        now=now,
    )

    # --- step 8: mark quote consumed ------------------------------------
    await QuoteRepository.mark_consumed(session, quote, execution_id=new_execution.id, now=now)

    # --- step 9: build response_body from POST-UPDATE balances ----------
    # debited_row.amount and credited_row.amount are the post-flush
    # values produced by the UPDATE the BalanceRepository just issued.
    # No separate read, no read-after-commit.
    response_body = _build_success_body(
        new_execution=new_execution,
        quote=quote,
        from_currency=from_currency,
        to_currency=to_currency,
        from_balance_after=debited_row.amount,
        to_balance_after=credited_row.amount,
    )
    await ExecutionRepository.finalize_succeeded(
        session, new_execution, response_body=response_body
    )

    return ExecuteOutcome(
        response_body=response_body,
        http_status=201,
        is_replay=False,
    )


async def _handle_idempotency_conflict(
    session: AsyncSession,
    request: ExecuteRequest,
) -> ExecuteOutcome:
    """The (customer_id, idempotency_key) row already exists.

    Same quote_id -> replay the stored response_body verbatim (HTTP 200).
    Different quote_id -> HTTP 409 with no DB state change.
    """
    existing = await ExecutionRepository.get_by_idempotency(
        session,
        customer_id=request.customer_id,
        idempotency_key=request.idempotency_key,
    )
    if existing is None:
        # The integrity error fired but the row isn't queryable -- racing
        # write or a different failure entirely. Surface it.
        raise RuntimeError("idempotency conflict reported but no existing execution row found")

    if existing.quote_id != request.quote_id:
        body = {
            "execution_id": str(existing.id),
            "quote_id": str(request.quote_id),
            "status": "failed",
            "failure_reason": FailureReason.IDEMPOTENCY_KEY_REUSED.value,
            "message": ("idempotency_key was previously used for a different quote_id"),
        }
        return ExecuteOutcome(response_body=body, http_status=409, is_replay=False)

    return ExecuteOutcome(
        response_body=existing.response_body,
        http_status=200,
        is_replay=True,
    )


async def _record_failure(
    session: AsyncSession,
    execution: Execution,
    *,
    failure_reason: FailureReason,
    http_status: int,
    message: str,
) -> ExecuteOutcome:
    body = {
        "execution_id": str(execution.id),
        "quote_id": str(execution.quote_id),
        "status": "failed",
        "failure_reason": failure_reason.value,
        "message": message,
    }
    await ExecutionRepository.finalize_failed(
        session, execution, failure_reason=failure_reason, response_body=body
    )
    return ExecuteOutcome(response_body=body, http_status=http_status, is_replay=False)


def _build_success_body(
    *,
    new_execution: Execution,
    quote: Quote,
    from_currency: Currency,
    to_currency: Currency,
    from_balance_after: Any,
    to_balance_after: Any,
) -> dict[str, Any]:
    return {
        "execution_id": str(new_execution.id),
        "quote_id": str(quote.id),
        "status": "succeeded",
        "debited": {
            "currency": from_currency.value,
            "amount": _quantize_str(quote.from_amount, from_currency),
        },
        "credited": {
            "currency": to_currency.value,
            "amount": _quantize_str(quote.to_amount, to_currency),
        },
        "balances_after": {
            from_currency.value: _quantize_str(from_balance_after, from_currency),
            to_currency.value: _quantize_str(to_balance_after, to_currency),
        },
    }
