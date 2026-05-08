"""POST /quotes -- price + persist a fresh quote.

Per SPEC §6 / §8: the route asks the rate provider for each leg
needed to price the request, which forces a refresh if the cache
is older than 60s. Pricing then reads the (now-refreshed) rates
table and produces the quote. If a leg is stale_unusable on both
the USD and EUR routes, pricing raises ``QuoteSourceUnavailable``
which the exception handler maps to HTTP 503.
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, Request, Response

from app.api.schemas import QuoteRequest, QuoteResponse
from app.domain.currency import Currency
from app.domain.rate_source import RateFetchFailed
from app.infra.repositories import QuoteRepository
from app.observability import QUOTE_TOTAL, get_logger
from app.services import quote_pricing

router = APIRouter()
log = get_logger(__name__)


def _legs_to_refresh(
    from_currency: Currency, to_currency: Currency
) -> list[tuple[Currency, Currency]]:
    """Return the (base, quote) pairs whose rate rows pricing will read.

    Direct pairs need their canonical row (base/quote alphabetical
    pairing as stored in the rates table). Cross pairs need both
    legs of both candidate routes (USD and EUR hubs).
    """
    if {from_currency, to_currency} == {Currency.KES, Currency.NGN}:
        return [
            (Currency.USD, Currency.KES),
            (Currency.USD, Currency.NGN),
            (Currency.EUR, Currency.KES),
            (Currency.EUR, Currency.NGN),
        ]
    # Direct or inverse-direct: refresh the canonical direct row.
    canonical: list[tuple[Currency, Currency]] = [
        (Currency.USD, Currency.KES),
        (Currency.USD, Currency.NGN),
        (Currency.USD, Currency.EUR),
        (Currency.EUR, Currency.KES),
        (Currency.EUR, Currency.NGN),
    ]
    for base, quote in canonical:
        if {base, quote} == {from_currency, to_currency}:
            return [(base, quote)]
    return []


@router.post("/quotes", response_model=QuoteResponse, status_code=201)
async def post_quotes(
    body: QuoteRequest,
    request: Request,
    response: Response,
) -> QuoteResponse:
    state = request.app.state
    rate_provider = state.rate_provider
    factory = state.session_factory
    clock = state.clock
    spread = state.spread

    from_currency = Currency(body.from_currency)
    to_currency = Currency(body.to_currency)

    # Refresh any pairs whose cached rate is older than 60s. Failures
    # fall through silently; pricing will raise QuoteSourceUnavailable
    # if no usable row remains.
    for base, quote in _legs_to_refresh(from_currency, to_currency):
        try:
            await rate_provider.get_rate(base=base, quote=quote)
        except RateFetchFailed:
            pass

    now: datetime = clock.now()
    async with factory() as session:
        async with session.begin():
            price = await quote_pricing(
                session,
                from_currency=from_currency,
                to_currency=to_currency,
                from_amount=body.from_amount,
                spread=spread,
                now=now,
            )

            from_amount_q = body.from_amount.quantize(_quantum(from_currency))
            to_amount_q = price.to_amount.quantize(_quantum(to_currency))

            quote_id = uuid4()
            quote = await QuoteRepository.create(
                session,
                quote_id=quote_id,
                customer_id=body.customer_id,
                from_currency=from_currency,
                to_currency=to_currency,
                from_amount=from_amount_q,
                to_amount=to_amount_q,
                rate_applied=price.rate_applied,
                routing=price.routing,
                now=now,
            )
            expires_at = quote.expires_at

    QUOTE_TOTAL.labels(routing=price.routing.value).inc()
    log.info(
        "quote.created",
        quote_id=str(quote_id),
        customer_id=str(body.customer_id),
        currency_pair=f"{from_currency.value}/{to_currency.value}",
        from_amount=str(from_amount_q),
        to_amount=str(to_amount_q),
        routing=price.routing.value,
    )

    return QuoteResponse(
        quote_id=quote_id,
        from_currency=from_currency.value,
        to_currency=to_currency.value,
        from_amount=from_amount_q,
        to_amount=to_amount_q,
        rate_applied=price.rate_applied,
        routing=price.routing.value,
        expires_at=expires_at,
    )


def _quantum(currency: Currency):
    from decimal import Decimal

    return Decimal(10) ** -currency.minor_units
