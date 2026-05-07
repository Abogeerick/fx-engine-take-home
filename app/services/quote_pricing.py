"""Quote pricing -- direct, inverse, and cross-pair routing.

Per SPEC §5 (spread model) and §8 (cross-pair staleness precedence).

Spread is supplied as a Decimal (e.g. ``Decimal("0.005")`` for 0.5%).
The customer-unfavourable factor ``(1 - spread)`` is applied to each
leg independently and compounded for cross pairs:

    direct:    rate = mid * (1 - s)
    inverse:   rate = (1 / mid) * (1 - s)
    cross:     rate = leg1_eff * leg2_eff  (each leg has (1 - s) applied)

Cross pairs (KES/NGN, NGN/KES) try the USD route first; on stale or
missing legs they fall back to EUR. If neither route has both legs
non-stale, the function raises ``QuoteSourceUnavailable`` (mapped to
HTTP 503 by the API layer).

All math is Decimal end-to-end; floats are forbidden per CLAUDE.md
hard rule §4.1.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.currency import Currency
from app.domain.quote import Routing
from app.domain.staleness import StalenessTier, classify
from app.infra.repositories import RateRepository

# Direct pairs as fetched from the upstream rate source. Inverses of
# any of these are derivable; everything else is a cross.
DIRECT_PAIRS: frozenset[tuple[Currency, Currency]] = frozenset(
    {
        (Currency.USD, Currency.KES),
        (Currency.USD, Currency.NGN),
        (Currency.USD, Currency.EUR),
        (Currency.EUR, Currency.KES),
        (Currency.EUR, Currency.NGN),
    }
)

# Cross pairs require routing through a hub. With our four currencies
# the only crosses are KES/NGN (and inverse).
_CROSS_HUBS: tuple[Currency, ...] = (Currency.USD, Currency.EUR)


class SameCurrencyError(ValueError):
    """from_currency == to_currency. API maps to HTTP 400."""


class QuoteSourceUnavailable(RuntimeError):
    """No usable rate row for the requested pair (or all routes stale).
    API maps to HTTP 503."""


@dataclass(frozen=True)
class PriceQuote:
    from_currency: Currency
    to_currency: Currency
    from_amount: Decimal
    to_amount: Decimal
    rate_applied: Decimal
    routing: Routing


def _is_cross(from_: Currency, to: Currency) -> bool:
    return {from_, to} == {Currency.KES, Currency.NGN}


async def _leg_rate_and_tier(
    session: AsyncSession,
    *,
    from_: Currency,
    to: Currency,
    now: datetime,
    spread_factor: Decimal,
) -> tuple[Decimal, StalenessTier] | None:
    """Return (effective from->to rate including spread, freshness tier)
    using either a direct rate row or its inverse, or None if no row
    exists for the pair in either direction."""
    direct = await RateRepository.get(session, base=from_, quote=to)
    if direct is not None:
        mid, fetched_at = direct
        return (mid * spread_factor, classify(fetched_at=fetched_at, now=now))

    inverse = await RateRepository.get(session, base=to, quote=from_)
    if inverse is not None:
        mid, fetched_at = inverse
        # mid is "from per to"; we want "to per from" -> 1/mid.
        return (
            (Decimal(1) / mid) * spread_factor,
            classify(fetched_at=fetched_at, now=now),
        )

    return None


async def quote_pricing(
    session: AsyncSession,
    *,
    from_currency: Currency,
    to_currency: Currency,
    from_amount: Decimal,
    spread: Decimal,
    now: datetime,
) -> PriceQuote:
    if from_currency == to_currency:
        raise SameCurrencyError(
            f"from_currency and to_currency must differ; got {from_currency.value}"
        )
    if from_amount <= 0:
        raise ValueError(f"from_amount must be positive; got {from_amount}")
    if spread < 0 or spread >= 1:
        raise ValueError(f"spread must be in [0, 1); got {spread}")

    spread_factor = Decimal(1) - spread

    if not _is_cross(from_currency, to_currency):
        leg = await _leg_rate_and_tier(
            session,
            from_=from_currency,
            to=to_currency,
            now=now,
            spread_factor=spread_factor,
        )
        if leg is None:
            # Should not happen with the four supported currencies, but
            # surface it if upstream rate population is incomplete.
            raise QuoteSourceUnavailable(
                f"no rate row available for {from_currency.value}/{to_currency.value}"
            )
        rate, tier = leg
        if tier == StalenessTier.STALE_UNUSABLE:
            raise QuoteSourceUnavailable(
                f"rate for {from_currency.value}/{to_currency.value} is stale_unusable"
            )
        return PriceQuote(
            from_currency=from_currency,
            to_currency=to_currency,
            from_amount=from_amount,
            to_amount=from_amount * rate,
            rate_applied=rate,
            routing=Routing.DIRECT,
        )

    # Cross pair -- try USD route, then EUR, per SPEC §8.
    for hub in _CROSS_HUBS:
        leg1 = await _leg_rate_and_tier(
            session,
            from_=from_currency,
            to=hub,
            now=now,
            spread_factor=spread_factor,
        )
        leg2 = await _leg_rate_and_tier(
            session,
            from_=hub,
            to=to_currency,
            now=now,
            spread_factor=spread_factor,
        )
        if leg1 is None or leg2 is None:
            continue
        rate1, tier1 = leg1
        rate2, tier2 = leg2
        if tier1 == StalenessTier.STALE_UNUSABLE or tier2 == StalenessTier.STALE_UNUSABLE:
            continue
        rate = rate1 * rate2
        routing = Routing.VIA_USD if hub == Currency.USD else Routing.VIA_EUR
        return PriceQuote(
            from_currency=from_currency,
            to_currency=to_currency,
            from_amount=from_amount,
            to_amount=from_amount * rate,
            rate_applied=rate,
            routing=routing,
        )

    raise QuoteSourceUnavailable(
        f"no usable cross route for {from_currency.value}/{to_currency.value}"
    )
