"""GET /healthz and GET /metrics.

Healthz reports rate-cache freshness across all five direct pairs:
``rate_source_state`` is the *worst* tier across them, so a single
stale-unusable pair degrades the overall status. Per SPEC §6 / §8.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.api.schemas import HealthResponse
from app.domain.currency import Currency
from app.domain.staleness import StalenessTier, classify
from app.infra.repositories import RateRepository

router = APIRouter()


_DIRECT_PAIRS: list[tuple[Currency, Currency]] = [
    (Currency.USD, Currency.KES),
    (Currency.USD, Currency.NGN),
    (Currency.USD, Currency.EUR),
    (Currency.EUR, Currency.KES),
    (Currency.EUR, Currency.NGN),
]

_TIER_RANK = {
    StalenessTier.FRESH: 0,
    StalenessTier.CACHED: 1,
    StalenessTier.STALE_UNUSABLE: 2,
}


@router.get("/healthz", response_model=HealthResponse)
async def healthz(request: Request) -> HealthResponse:
    state = request.app.state
    factory = state.session_factory
    clock = state.clock
    now = clock.now()

    oldest_age_seconds: int | None = None
    worst_tier = StalenessTier.FRESH
    any_present = False

    async with factory() as session:
        for base, quote in _DIRECT_PAIRS:
            row = await RateRepository.get(session, base=base, quote=quote)
            if row is None:
                worst_tier = StalenessTier.STALE_UNUSABLE
                continue
            any_present = True
            _, fetched_at = row
            age_s = int((now - fetched_at).total_seconds())
            if oldest_age_seconds is None or age_s > oldest_age_seconds:
                oldest_age_seconds = age_s
            tier = classify(fetched_at=fetched_at, now=now)
            if _TIER_RANK[tier] > _TIER_RANK[worst_tier]:
                worst_tier = tier

    if not any_present:
        worst_tier = StalenessTier.STALE_UNUSABLE

    return HealthResponse(
        status="ok" if worst_tier != StalenessTier.STALE_UNUSABLE else "degraded",
        rate_cache_age_seconds=oldest_age_seconds,
        rate_source_state=worst_tier.value,
    )


@router.get("/metrics")
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
