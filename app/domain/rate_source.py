"""``RateSource`` Protocol -- the abstract contract for fetching mid-rates.

The Protocol is what pure-domain code depends on. Concrete
implementations live in ``app/infra/rate_provider/``: the production
``ExchangeRatesApiSource`` (httpx) and any test fakes a caller cares
to write.

The ``now`` parameter is passed in rather than read from the wall
clock so tests can drive the source with a ``FrozenClock`` without
patching ``datetime``.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.domain.currency import Currency


class RateFetchFailed(Exception):
    """The upstream rate source could not return a usable answer.

    Subclasses or callers should not catch this and pretend the call
    succeeded -- the circuit breaker treats it as a failure for state
    accounting.
    """


class RateSource(Protocol):
    async def get_rate(
        self,
        *,
        base: Currency,
        quote: Currency,
        now: datetime,
    ) -> tuple[Decimal, datetime, str]:
        """Fetch the current mid-rate for ``base`` / ``quote``.

        Returns ``(mid_rate, fetched_at, source_name)``. ``mid_rate``
        is in the SPEC convention -- quote-per-base. ``fetched_at`` is
        tz-aware UTC, typically equal to or near ``now``.
        Raises ``RateFetchFailed`` on any upstream error.
        """
        ...
