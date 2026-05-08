"""Concurrent workload smoke test against a live local API.

Spawns ``--customers`` workers; each creates a customer, credits a
USD balance, then loops ``--quotes-per-customer`` times: post a
quote, execute it. Reports total requests, success rate, and
p50/p95/p99 latency per request type.

Pure asyncio + httpx -- no external load tools.

Usage:
    python -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000 &
    python scripts/load_test.py --customers 10 --quotes-per-customer 5
"""

from __future__ import annotations

import argparse
import asyncio
import time
from collections import defaultdict
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


async def _timed(
    coro_factory: Any, latencies: list[float], errors: dict[int, int]
) -> dict[str, Any] | None:
    start = time.perf_counter()
    try:
        response = await coro_factory()
        latencies.append((time.perf_counter() - start) * 1000)
        if 200 <= response.status_code < 300:
            return response.json() if response.content else None
        errors[response.status_code] += 1
        return None
    except Exception:
        errors[599] += 1
        latencies.append((time.perf_counter() - start) * 1000)
        return None


async def _run_customer(
    client: httpx.AsyncClient,
    quotes_per_customer: int,
    quote_lat: list[float],
    execute_lat: list[float],
    quote_errors: dict[int, int],
    execute_errors: dict[int, int],
) -> None:
    # Create customer.
    r = await client.post("/customers", json={})
    if r.status_code != 201:
        return
    cid = r.json()["customer_id"]

    # Credit a comfortable balance.
    await client.post(
        f"/customers/{cid}/credit",
        json={"currency": "USD", "amount": str(Decimal("100000"))},
    )

    for _ in range(quotes_per_customer):
        quote = await _timed(
            lambda cid=cid: client.post(
                "/quotes",
                json={
                    "customer_id": cid,
                    "from_currency": "USD",
                    "to_currency": "KES",
                    "from_amount": "100",
                },
            ),
            quote_lat,
            quote_errors,
        )
        if quote is None:
            continue

        # Default-arg binding pins the per-iteration values into the lambda
        # so the loop variable doesn't get captured by reference.
        await _timed(
            lambda cid=cid, quote=quote: client.post(
                "/executions",
                json={
                    "quote_id": quote["quote_id"],
                    "customer_id": cid,
                    "idempotency_key": str(uuid4()),
                },
            ),
            execute_lat,
            execute_errors,
        )


async def main(args: argparse.Namespace) -> None:
    quote_lat: list[float] = []
    execute_lat: list[float] = []
    quote_errors: dict[int, int] = defaultdict(int)
    execute_errors: dict[int, int] = defaultdict(int)

    async with httpx.AsyncClient(base_url=args.url, timeout=30.0) as client:
        start = time.perf_counter()
        tasks = [
            _run_customer(
                client,
                args.quotes_per_customer,
                quote_lat,
                execute_lat,
                quote_errors,
                execute_errors,
            )
            for _ in range(args.customers)
        ]
        await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - start

    total_quotes = len(quote_lat)
    total_executes = len(execute_lat)
    print("=== fx-engine load test ===")
    print(f"customers:           {args.customers}")
    print(f"quotes per customer: {args.quotes_per_customer}")
    print(f"wall time:           {elapsed:.2f}s")
    print()
    print(f"quote requests:      {total_quotes}")
    print(f"  errors by code:    {dict(quote_errors) if quote_errors else 'none'}")
    if quote_lat:
        print(
            f"  latency p50/p95/p99 ms: "
            f"{_percentile(quote_lat, 50):.1f} / "
            f"{_percentile(quote_lat, 95):.1f} / "
            f"{_percentile(quote_lat, 99):.1f}"
        )
    print()
    print(f"execute requests:    {total_executes}")
    print(f"  errors by code:    {dict(execute_errors) if execute_errors else 'none'}")
    if execute_lat:
        print(
            f"  latency p50/p95/p99 ms: "
            f"{_percentile(execute_lat, 50):.1f} / "
            f"{_percentile(execute_lat, 95):.1f} / "
            f"{_percentile(execute_lat, 99):.1f}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--customers", type=int, default=10)
    parser.add_argument("--quotes-per-customer", type=int, default=5)
    asyncio.run(main(parser.parse_args()))
