"""Rate provider package -- composes the source, cache, breaker, and
singleflight into a single ``RateProvider`` class.

Read flow per SPEC §8::

    1. Read cache. If FRESH, return immediately (no fetch).
    2. Otherwise, attempt fresh fetch through breaker -> singleflight.
       The fetch upserts the cache on success.
    3. Re-read cache. If a row exists (newly upserted, or pre-existing
       and now in the CACHED tier because the fetch failed), return it.
    4. If the cache is still empty, raise -- there's nothing to serve.

The fall-through-to-cache path is what makes the breaker valuable:
during the breaker-open window, every request returns the last known
value with the staleness tier reflecting its age. SPEC §8 requires
this to fail closed (HTTP 503) only when the cache itself is
``stale_unusable``; that's the API layer's job (step 5) to translate
the returned tier to a status code.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.domain.clock import Clock
from app.domain.currency import Currency
from app.domain.rate_source import RateFetchFailed, RateSource
from app.domain.staleness import StalenessTier
from app.infra.rate_provider.cache import CacheEntry, RateCache
from app.infra.rate_provider.circuit_breaker import (
    CircuitBreaker,
    OpenCircuitError,
)
from app.infra.rate_provider.exchangeratesapi import ExchangeRatesApiSource
from app.infra.rate_provider.scheduler import RateRefreshScheduler
from app.infra.rate_provider.singleflight import Singleflight

__all__ = [
    "CacheEntry",
    "CircuitBreaker",
    "ExchangeRatesApiSource",
    "OpenCircuitError",
    "RateCache",
    "RateInfo",
    "RateProvider",
    "RateRefreshScheduler",
    "Singleflight",
]


@dataclass(frozen=True)
class RateInfo:
    mid_rate: Decimal
    fetched_at: datetime
    tier: StalenessTier
    source: str


class RateProvider:
    def __init__(
        self,
        *,
        source: RateSource,
        cache: RateCache,
        circuit_breaker: CircuitBreaker,
        singleflight: Singleflight[tuple[Currency, Currency], None],
        clock: Clock,
    ) -> None:
        self._source = source
        self._cache = cache
        self._cb = circuit_breaker
        self._sf = singleflight
        self._clock = clock

    async def get_rate(
        self,
        *,
        base: Currency,
        quote: Currency,
    ) -> RateInfo:
        now = self._clock.now()
        cached = await self._cache.get(base=base, quote=quote, now=now)

        # Fresh cache: short-circuit, no upstream call.
        if cached is not None and cached.tier == StalenessTier.FRESH:
            return RateInfo(
                mid_rate=cached.mid_rate,
                fetched_at=cached.fetched_at,
                tier=cached.tier,
                source="cache",
            )

        # Cache stale or missing: try a fresh fetch through cb + sf.
        # The breaker may reject the call (open). Either path falls
        # through to whatever the cache currently holds.
        try:
            await self._cb.call(self._coalesced_fetch_factory(base, quote))
        except (OpenCircuitError, RateFetchFailed):
            pass

        cached = await self._cache.get(base=base, quote=quote, now=self._clock.now())
        if cached is None:
            raise RateFetchFailed(f"no rate available for {base.value}/{quote.value}")
        return RateInfo(
            mid_rate=cached.mid_rate,
            fetched_at=cached.fetched_at,
            tier=cached.tier,
            source="upstream" if cached.tier == StalenessTier.FRESH else "cache",
        )

    def _coalesced_fetch_factory(
        self, base: Currency, quote: Currency
    ) -> Callable[[], Awaitable[None]]:
        async def coalesced() -> None:
            await self._sf.do(
                (base, quote),
                self._fetch_and_upsert_factory(base, quote),
            )

        return coalesced

    def _fetch_and_upsert_factory(
        self, base: Currency, quote: Currency
    ) -> Callable[[], Awaitable[None]]:
        async def fetch_and_upsert() -> None:
            mid_rate, fetched_at, source_name = await self._source.get_rate(
                base=base, quote=quote, now=self._clock.now()
            )
            await self._cache.upsert(
                base=base,
                quote=quote,
                mid_rate=mid_rate,
                fetched_at=fetched_at,
                source=source_name,
            )

        return fetch_and_upsert
