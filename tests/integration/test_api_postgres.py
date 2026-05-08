"""End-to-end API tests against the FastAPI app.

The app runs in-process via ``httpx.ASGITransport`` so we exercise
the full middleware + route + service + DB stack without spinning
up a real server. The lifespan handler is driven manually so the
scheduler starts (and is stopped on cleanup) just like in production.

The rate provider is monkey-patched onto a controllable fake source
so tests don't depend on exchangeratesapi.io being reachable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api.main import create_app
from app.domain.currency import Currency
from app.domain.rate_source import RateFetchFailed


class _FakeSource:
    SOURCE_NAME = "fake-api"

    def __init__(self) -> None:
        self.rates: dict[tuple[Currency, Currency], Decimal] = {
            (Currency.USD, Currency.KES): Decimal("130"),
            (Currency.USD, Currency.NGN): Decimal("1500"),
            (Currency.USD, Currency.EUR): Decimal("0.92"),
            (Currency.EUR, Currency.KES): Decimal("141.30"),
            (Currency.EUR, Currency.NGN): Decimal("1630.43"),
        }

    async def get_rate(
        self,
        *,
        base: Currency,
        quote: Currency,
        now: datetime,
    ) -> tuple[Decimal, datetime, str]:
        rate = self.rates.get((base, quote))
        if rate is None:
            raise RateFetchFailed(f"no fake rate for {base.value}/{quote.value}")
        return (rate, now, self.SOURCE_NAME)


@pytest_asyncio.fixture
async def api_client(monkeypatch, postgres_url: str) -> AsyncIterator[AsyncClient]:
    """Build the app, swap the rate source for a fake, drive lifespan."""
    monkeypatch.setenv("DATABASE_URL", postgres_url)
    monkeypatch.setenv("ENV", "test")
    # Ensure get_settings() rebuilds with the patched env.
    from app.infra.config import get_settings

    get_settings.cache_clear()

    app = create_app()
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        # Swap the rate provider's source to the fake AFTER lifespan has
        # constructed the provider.
        app.state.rate_provider._source = _FakeSource()  # type: ignore[attr-defined]
        try:
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                yield client
        finally:
            # The api_client fixture builds its own engine via lifespan;
            # truncate so subsequent tests in the integration suite don't
            # see leftover state.
            from sqlalchemy import text

            async with app.state.engine.begin() as conn:
                await conn.execute(
                    text(
                        "TRUNCATE ledger_entries, executions, quotes, "
                        "balances, customers, rates RESTART IDENTITY CASCADE"
                    )
                )


# --- /healthz --------------------------------------------------------------


async def test_healthz_returns_state_shape(api_client: AsyncClient) -> None:
    r = await api_client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert "status" in body
    assert "rate_cache_age_seconds" in body
    assert "rate_source_state" in body
    assert body["rate_source_state"] in {"fresh", "cached", "stale_unusable"}


# --- /customers + /credit (env-gated) -------------------------------------


async def test_create_customer_and_credit_flow(api_client: AsyncClient) -> None:
    r = await api_client.post("/customers", json={})
    assert r.status_code == 201
    cid = r.json()["customer_id"]

    r = await api_client.post(
        f"/customers/{cid}/credit",
        json={"currency": "USD", "amount": "1000"},
    )
    assert r.status_code == 200
    assert r.json()["new_balance"] == "1000.00000000" or r.json()["new_balance"] == "1000"

    r = await api_client.get(f"/customers/{cid}/balances")
    assert r.status_code == 200
    body = r.json()
    assert body["customer_id"] == cid
    assert body["balances"]["USD"] == "1000.00"


async def test_credit_rejects_zero_or_negative(api_client: AsyncClient) -> None:
    r = await api_client.post("/customers", json={})
    cid = r.json()["customer_id"]

    r = await api_client.post(
        f"/customers/{cid}/credit",
        json={"currency": "USD", "amount": "0"},
    )
    assert r.status_code == 400


# --- /quotes ---------------------------------------------------------------


async def test_post_quote_returns_string_decimals(api_client: AsyncClient) -> None:
    r = await api_client.post("/customers", json={})
    cid = r.json()["customer_id"]

    r = await api_client.post(
        "/quotes",
        json={
            "customer_id": cid,
            "from_currency": "USD",
            "to_currency": "KES",
            "from_amount": "100",
        },
    )
    assert r.status_code == 201
    body = r.json()

    # Decimal fields are strings, not numbers, per SPEC §6.
    for key in ("from_amount", "to_amount", "rate_applied"):
        assert isinstance(body[key], str), f"{key} must be string"
    assert body["from_amount"] == "100.00"
    assert body["routing"] == "direct"


async def test_post_quote_lowercase_currency_rejected(
    api_client: AsyncClient,
) -> None:
    r = await api_client.post("/customers", json={})
    cid = r.json()["customer_id"]

    r = await api_client.post(
        "/quotes",
        json={
            "customer_id": cid,
            "from_currency": "usd",
            "to_currency": "KES",
            "from_amount": "100",
        },
    )
    assert r.status_code == 400


async def test_post_quote_same_currency_rejected(api_client: AsyncClient) -> None:
    r = await api_client.post("/customers", json={})
    cid = r.json()["customer_id"]
    r = await api_client.post(
        "/quotes",
        json={
            "customer_id": cid,
            "from_currency": "USD",
            "to_currency": "USD",
            "from_amount": "100",
        },
    )
    assert r.status_code == 400


# --- /executions -----------------------------------------------------------


async def test_full_quote_then_execute_flow(api_client: AsyncClient) -> None:
    r = await api_client.post("/customers", json={})
    cid = r.json()["customer_id"]
    await api_client.post(
        f"/customers/{cid}/credit",
        json={"currency": "USD", "amount": "1000"},
    )

    r = await api_client.post(
        "/quotes",
        json={
            "customer_id": cid,
            "from_currency": "USD",
            "to_currency": "KES",
            "from_amount": "100",
        },
    )
    quote = r.json()

    r = await api_client.post(
        "/executions",
        json={
            "quote_id": quote["quote_id"],
            "customer_id": cid,
            "idempotency_key": "first-key",
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "succeeded"
    assert body["debited"]["currency"] == "USD"
    assert body["credited"]["currency"] == "KES"

    # Idempotent replay: same key -> 200 with byte-identical body.
    r2 = await api_client.post(
        "/executions",
        json={
            "quote_id": quote["quote_id"],
            "customer_id": cid,
            "idempotency_key": "first-key",
        },
    )
    assert r2.status_code == 200
    assert r2.json() == body


async def test_correlation_id_is_echoed_back(api_client: AsyncClient) -> None:
    r = await api_client.get(
        "/healthz",
        headers={"X-Correlation-ID": "test-correlation-12345"},
    )
    assert r.headers.get("X-Correlation-ID") == "test-correlation-12345"


async def test_correlation_id_generated_when_absent(api_client: AsyncClient) -> None:
    r = await api_client.get("/healthz")
    cid = r.headers.get("X-Correlation-ID")
    assert cid is not None
    # Should look like a UUID4
    assert len(cid) == 36


# --- /metrics --------------------------------------------------------------


async def test_metrics_exposes_prometheus_format(api_client: AsyncClient) -> None:
    # Drive a quote so the counter ticks.
    r = await api_client.post("/customers", json={})
    cid = r.json()["customer_id"]
    await api_client.post(
        f"/customers/{cid}/credit",
        json={"currency": "USD", "amount": "1000"},
    )
    await api_client.post(
        "/quotes",
        json={
            "customer_id": cid,
            "from_currency": "USD",
            "to_currency": "KES",
            "from_amount": "100",
        },
    )

    r = await api_client.get("/metrics")
    assert r.status_code == 200
    body = r.text
    assert "fx_quote_total" in body
