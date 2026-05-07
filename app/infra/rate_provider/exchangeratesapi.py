"""``ExchangeRatesApiSource`` -- httpx-backed implementation of ``RateSource``.

Per AC #2: this class does NOT retry. Retries are the circuit
breaker's concern; failures propagate as ``RateFetchFailed`` with
the original exception attached.

The free tier of exchangeratesapi.io fixes ``base`` to EUR. Other
bases either fail upstream (the breaker handles it) or are derived
from a EUR base by the calling layer. This implementation passes
the requested base through unchanged so paid-tier deployments work
without code changes.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import httpx

from app.domain.currency import Currency
from app.domain.rate_source import RateFetchFailed


class ExchangeRatesApiSource:
    SOURCE_NAME = "exchangeratesapi.io"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: float = 5.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout

    async def get_rate(
        self,
        *,
        base: Currency,
        quote: Currency,
        now: datetime,
    ) -> tuple[Decimal, datetime, str]:
        url = f"{self._base_url}/latest"
        params = {
            "access_key": self._api_key,
            "base": base.value,
            "symbols": quote.value,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPError as exc:
            raise RateFetchFailed(
                f"upstream HTTP error fetching {base.value}/{quote.value}: {exc}"
            ) from exc

        if not isinstance(data, dict):
            raise RateFetchFailed(f"unexpected upstream response type: {type(data)!r}")

        # exchangeratesapi.io's free tier returns {"success": true, "rates": {...}}
        # but some endpoints omit the success flag. Treat absence as success.
        if data.get("success") is False:
            raise RateFetchFailed(f"upstream reported failure: {data.get('error', data)}")

        rates = data.get("rates")
        if not isinstance(rates, dict) or quote.value not in rates:
            raise RateFetchFailed(f"upstream response missing rate for {quote.value}: {data!r}")

        # Decimal(str(...)) goes through the string representation, avoiding
        # float-precision contamination from the JSON parser's float result.
        try:
            mid_rate = Decimal(str(rates[quote.value]))
        except (ValueError, ArithmeticError) as exc:
            raise RateFetchFailed(
                f"upstream rate for {quote.value} not Decimal-parseable: {rates[quote.value]!r}"
            ) from exc

        if mid_rate <= 0:
            raise RateFetchFailed(f"upstream returned non-positive rate: {mid_rate}")

        return (mid_rate, now, self.SOURCE_NAME)
