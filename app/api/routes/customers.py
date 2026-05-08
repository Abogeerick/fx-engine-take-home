"""Customer routes.

``GET /customers/{id}/balances`` is production-safe; the create and
credit endpoints are test fixtures gated by ``ENV != production``.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request

from app.api.schemas import (
    BalancesResponse,
    CreditRequest,
    CreditResponse,
    CustomerCreate,
    CustomerCreated,
)
from app.domain.currency import Currency
from app.domain.money import Money
from app.infra.repositories import BalanceRepository, CustomerRepository

router = APIRouter()


def _ensure_not_production(request: Request) -> None:
    if request.app.state.settings.env == "production":
        raise HTTPException(status_code=404, detail="endpoint not available in production")


def _format_amount(amount: Decimal, currency: Currency) -> str:
    return str(Money(amount=amount, currency=currency).quantize_to_minor_units().amount)


@router.post("/customers", response_model=CustomerCreated, status_code=201)
async def create_customer(
    body: CustomerCreate,
    request: Request,
) -> CustomerCreated:
    _ensure_not_production(request)
    cid = body.customer_id if body.customer_id is not None else uuid4()

    factory = request.app.state.session_factory
    async with factory() as session:
        async with session.begin():
            await CustomerRepository.create(session, cid)

    return CustomerCreated(customer_id=cid)


@router.post("/customers/{customer_id}/credit", response_model=CreditResponse)
async def credit_customer(
    customer_id: UUID,
    body: CreditRequest,
    request: Request,
) -> CreditResponse:
    _ensure_not_production(request)
    if body.amount <= 0:
        raise HTTPException(status_code=400, detail="amount must be positive")

    currency = Currency(body.currency)
    factory = request.app.state.session_factory
    async with factory() as session:
        async with session.begin():
            row = await BalanceRepository.credit(
                session,
                customer_id,
                Money(amount=body.amount, currency=currency),
            )
            new_balance = row.amount

    return CreditResponse(
        customer_id=customer_id,
        currency=currency.value,
        new_balance=new_balance,
    )


@router.get("/customers/{customer_id}/balances", response_model=BalancesResponse)
async def get_balances(
    customer_id: UUID,
    request: Request,
) -> BalancesResponse:
    factory = request.app.state.session_factory
    async with factory() as session:
        rows = await BalanceRepository.get_all(session, customer_id)

    return BalancesResponse(
        customer_id=customer_id,
        balances={r.currency: _format_amount(r.amount, Currency(r.currency)) for r in rows},
    )
