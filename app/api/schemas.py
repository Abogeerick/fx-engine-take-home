"""Pydantic v2 request and response schemas.

All Decimal values are serialised as JSON **strings**, never numbers.
JSON numbers are floats per the spec; SPEC §6 examples show string
serialisation (``"100.00"``). The ``DecimalStr`` annotated type
enforces this both inbound (rejects float inputs) and outbound
(serialises via ``str``).

Currency codes are validated through ``Currency.from_code`` so
lowercase / unknown codes are rejected with a 400 at the API
boundary (per SPEC §2 -- silent normalisation hides client bugs).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer

from app.domain.currency import Currency


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    # bool is a subclass of int -- reject before the int branch.
    if isinstance(value, bool):
        raise TypeError("Decimal value cannot be a bool")
    if isinstance(value, str):
        try:
            return Decimal(value)
        except ArithmeticError as exc:
            raise ValueError(f"invalid decimal string: {value!r}") from exc
    if isinstance(value, int):
        return Decimal(value)
    raise TypeError(f"Decimal value must be string or int (no floats); got {type(value).__name__}")


def _validate_currency(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError(f"currency must be str; got {type(value).__name__}")
    return Currency.from_code(value).value


DecimalStr = Annotated[
    Decimal,
    BeforeValidator(_to_decimal),
    PlainSerializer(str, return_type=str, when_used="json"),
]

CurrencyCode = Annotated[str, BeforeValidator(_validate_currency)]


# --- /quotes ---------------------------------------------------------------


class QuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: UUID
    from_currency: CurrencyCode
    to_currency: CurrencyCode
    from_amount: DecimalStr


class QuoteResponse(BaseModel):
    quote_id: UUID
    from_currency: CurrencyCode
    to_currency: CurrencyCode
    from_amount: DecimalStr
    to_amount: DecimalStr
    rate_applied: DecimalStr
    routing: str
    expires_at: datetime


# --- /executions -----------------------------------------------------------


class ExecuteRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quote_id: UUID
    customer_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=255)


# Execute responses are dict-shaped (success body or failure body) so the
# orchestrator's stored response_body can be replayed verbatim. The route
# returns whatever the orchestrator produced.


# --- /customers ------------------------------------------------------------


class CustomerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: UUID | None = None


class CustomerCreated(BaseModel):
    customer_id: UUID


class CreditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    currency: CurrencyCode
    amount: DecimalStr


class CreditResponse(BaseModel):
    customer_id: UUID
    currency: CurrencyCode
    new_balance: DecimalStr


class BalancesResponse(BaseModel):
    customer_id: UUID
    balances: dict[str, str]  # currency code -> minor-unit string-decimal


# --- /admin ----------------------------------------------------------------


class RefreshResponse(BaseModel):
    refreshed_pairs: list[str]
    failed_pairs: list[str]


# --- /healthz --------------------------------------------------------------


class HealthResponse(BaseModel):
    status: str
    rate_cache_age_seconds: int | None
    rate_source_state: str
