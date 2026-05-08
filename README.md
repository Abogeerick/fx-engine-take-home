# Umba FX Engine

A foreign-exchange engine that issues short-lived quotes and atomically
executes them against per-customer multi-currency balances. Built for
the Umba senior backend engineer take-home.

The technical spec is in [`docs/SPEC.md`](docs/SPEC.md). The agent
operating instructions used during build are in [`CLAUDE.md`](CLAUDE.md).
The build's design log is in [`docs/DECISIONS.md`](docs/DECISIONS.md).

## Setup

Requires Python 3.11+ (developed against 3.13) and Docker (for
Postgres). All other deps are in `pyproject.toml`.

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows; or `source .venv/bin/activate` on POSIX
pip install -e ".[dev]"

# Bring up Postgres for integration / production-tier tests.
POSTGRES_PASSWORD=devpass docker compose up -d --wait postgres
```

Copy `.env.example` to `.env` and fill in any keys you have (the
engine runs without an `RATE_API_KEY` -- the rate provider then
returns `stale_unusable` and quote endpoints return 503, which is
the correct behaviour per SPEC §8).

## Running tests

The Makefile drives the four test tiers:

```bash
make test-unit          # SQLite + pure-domain; fastest. ~80 tests.
make test-integration   # Postgres-required; concurrency, FOR UPDATE,
                        # CHECK constraints, atomic execute. ~25 tests.
make test               # both, in order.
make lint               # ruff
make typecheck          # mypy strict on app/domain
```

The Hypothesis property test for Decimal precision invariants lives
in `tests/property/`; it runs with the unit tier.

## Running the API locally

```bash
DATABASE_URL=postgresql+asyncpg://fx:devpass@localhost:5433/fx_engine \
ENV=development \
python -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000
```

Then exercise it:

```bash
# Create a customer, fund it, quote, execute.
curl -X POST localhost:8000/customers -H 'content-type: application/json' -d '{}'
# -> {"customer_id":"<uuid>"}

curl -X POST "localhost:8000/customers/<uuid>/credit" \
     -H 'content-type: application/json' \
     -d '{"currency":"USD","amount":"1000"}'

curl -X POST localhost:8000/quotes \
     -H 'content-type: application/json' \
     -d '{"customer_id":"<uuid>","from_currency":"USD","to_currency":"KES","from_amount":"100"}'
# -> {"quote_id":"<uuid>","from_amount":"100.00","to_amount":"12935.00",...}

curl -X POST localhost:8000/executions \
     -H 'content-type: application/json' \
     -d '{"quote_id":"<uuid>","customer_id":"<uuid>","idempotency_key":"first-attempt"}'
# -> 201 with debited / credited / balances_after
```

`/healthz` reports rate-cache freshness; `/metrics` exposes
prometheus counters (quote count, execute count by status, rate-fetch
latency, idempotent-replay count).

## Load test

`scripts/load_test.py` runs a small concurrent workload against a
running API. Pure asyncio + httpx; no external load tools.

```bash
# In one shell:
DATABASE_URL=postgresql+asyncpg://fx:devpass@localhost:5433/fx_engine \
ENV=development \
python -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000

# In another shell, after seeding rates (e.g. via `make migrate` and
# the admin endpoint, or by inserting via psql):
python scripts/load_test.py --customers 10 --quotes-per-customer 5
```

Sample output from the local run captured during the build:

```
=== fx-engine load test ===
customers:           10
quotes per customer: 5
wall time:           2.09s

quote requests:      50
  errors by code:    none
  latency p50/p95/p99 ms: 114.2 / 228.9 / 270.2

execute requests:    50
  errors by code:    none
  latency p50/p95/p99 ms: 167.0 / 297.7 / 321.3
```

## Sample log output

Every request emits structured JSON with a correlation ID that
threads through every event raised during the request, so a
quote -> execute trace can be reconstructed from logs (per SPEC §9):

```
{"method": "POST", "path": "/quotes", "status_code": 201, "latency_ms": 31.7,
 "event": "request.completed",
 "correlation_id": "ede0b1cd-529c-476d-a5e7-2f0e79149365",
 "level": "info", "timestamp": "2026-05-08T05:40:02.390214Z"}

{"quote_id": "a1162869-71c1-4034-8d66-fe425b791dbe",
 "execution_id": "5495de55-43ae-4395-a74b-cd61c442a2a5",
 "customer_id": "9d4a0299-72eb-4c69-b0bc-ecdad6016d14",
 "http_status": 201, "outcome": "succeeded", "is_replay": false,
 "event": "execute.completed",
 "correlation_id": "ede0b1cd-529c-476d-a5e7-2f0e79149365",
 "level": "info", "timestamp": "2026-05-08T05:40:02.416425Z"}
```

The two events share `correlation_id`, so a quote->execute pair can
be joined by that field.

## Repository layout

```
fx-engine/
├── app/
│   ├── api/              FastAPI routes, schemas, middleware, exception map
│   ├── domain/           Pure logic: Money, Currency, Clock, Quote, staleness
│   ├── infra/            DB, models, repositories, rate provider, config
│   └── observability/    structlog config, prometheus metrics
├── tests/
│   ├── unit/             SQLite + pure-domain tests
│   ├── property/         Hypothesis property tests
│   ├── concurrency/      (folded into integration)
│   └── integration/      Postgres-required tests
├── alembic/              migrations
├── scripts/
│   └── load_test.py      asyncio + httpx load script
├── docs/
│   ├── SPEC.md           technical specification (v0.2)
│   ├── DECISIONS.md      design log; condensed for submission
│   └── REVIEW.md         planted-bugs review (added in step 6)
└── planted_bugs/         the assignment-provided code under review
```

## Known limitations

* **Singleflight scope is in-process** (per SPEC §8). In a multi-worker
  deployment, up to N upstream calls per refresh window can occur.
  Production would use a DB advisory lock or Redis SETNX for global
  singleflight. Documented in DECISIONS.md.
* **Cross-table FK** from `quotes.consumed_by_execution_id` to
  `executions.id` is enforced only at the ORM layer, not in the DB.
  SQLite cannot `ALTER TABLE ADD CONSTRAINT FOREIGN KEY` on existing
  tables and the `quotes` table is created before `executions` in
  the migration. The integrity argument is that the only writer is
  the execute orchestrator, in the same transaction as the executions
  insert.
* **Step ordering deviation from SPEC §7.** The orchestrator takes
  `SELECT FOR UPDATE` on the quote BEFORE inserting the executions
  row. The literal SPEC ordering deadlocks on Postgres under N
  parallel executes (FK lock vs FOR UPDATE upgrade). All observable
  behaviours are preserved. Documented in DECISIONS.md and inline in
  `app/services/execute.py`.
* **Rate API key**: the engine's tests pass without a real
  `RATE_API_KEY` because they fake the rate source. A live deployment
  needs a paid exchangeratesapi.io key; the free tier locks `base` to
  EUR.

## What I'd do with another day

* **Per-pair, per-tier spreads.** SPEC §13 calls this out as a real
  fintech requirement; the code is structured for it (a per-pair
  lookup table) but the current spread is a single `Decimal` config
  value.
* **Global singleflight.** A DB advisory lock would reduce upstream
  fan-out under multi-worker deployments to 1 per refresh window, not
  N.
* **Property test on the cross-pair compounding identity.** The
  current Hypothesis test asserts post-trade balances match the
  rounded amounts. A separate property could assert that the cross
  rate equals `leg1 * leg2 * (1 - s)^2` to within Decimal precision
  for any randomised pair of mid-rates.
* **Failed-execution row retention policy.** Sticky failures
  accumulate forever. A real deployment would TTL them after some
  retention window or move them to a cold-storage table.

## Estimated time

* Wall-clock: roughly Wednesday afternoon through Friday evening.
* Active engagement: ~12-15 hours, broken into five named build steps
  (see git log) with explicit AC review at each commit.
