"""POST /admin/rates/refresh -- force a refresh of all direct pairs.

Token-gated in production (``X-Admin-Token`` header must match
``ADMIN_TOKEN`` env). Open in dev / test environments.
"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request

from app.api.schemas import RefreshResponse
from app.domain.currency import Currency
from app.domain.rate_source import RateFetchFailed

router = APIRouter()


_DIRECT_PAIRS: list[tuple[Currency, Currency]] = [
    (Currency.USD, Currency.KES),
    (Currency.USD, Currency.NGN),
    (Currency.USD, Currency.EUR),
    (Currency.EUR, Currency.KES),
    (Currency.EUR, Currency.NGN),
]


@router.post("/admin/rates/refresh", response_model=RefreshResponse)
async def admin_refresh_rates(
    request: Request,
    x_admin_token: str | None = Header(None),
) -> RefreshResponse:
    settings = request.app.state.settings
    if settings.env == "production":
        if not settings.admin_token or x_admin_token != settings.admin_token:
            raise HTTPException(status_code=401, detail="invalid admin token")

    rate_provider = request.app.state.rate_provider
    refreshed: list[str] = []
    failed: list[str] = []

    for base, quote in _DIRECT_PAIRS:
        try:
            await rate_provider.get_rate(base=base, quote=quote)
            refreshed.append(f"{base.value}/{quote.value}")
        except RateFetchFailed:
            failed.append(f"{base.value}/{quote.value}")

    return RefreshResponse(refreshed_pairs=refreshed, failed_pairs=failed)
