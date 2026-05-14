# Umba FX Engine

A foreign-exchange engine that issues short-lived quotes between USD,
EUR, KES, and NGN and atomically executes them against per-customer
multi-currency balances. Built for the Umba senior backend engineer
take-home.

## How to grade this submission

The artefacts to read, in this order:

1. **[`docs/SPEC.md`](docs/SPEC.md)** — the technical specification I
   wrote before any code. v0.2 includes the clarifications surfaced
   by the agent's read-back; the original v0.1 is in the git history
   if you want to compare.
2. **[`docs/DECISIONS.md`](docs/DECISIONS.md)** — one-page summary
   of trade-offs, what I delegated vs owned, what I overrode and
   why, the SPEC §7 deviation, the four bugs strict tooling caught,
   and what the AI got wrong (with how I caught it). The longer
   running log is in `docs/DECISIONS_LOG.md` for transparency.
3. **[`docs/REVIEW.md`](docs/REVIEW.md)** — the planted-bugs review
   per Part 3. Nine bugs ranked by production impact, four
   deliberately-not-flagged with rationale, reproductions in
   `scratch/` (gitignored, not committed).
4. **`git log --oneline`** — eleven scoped commits telling the
   build narrative end to end. Each commit's body explains the why.

## Quick start

Requires **Python 3.13** (the venv was developed and tested on 3.13;
3.14 hits a pytest-asyncio deprecation that's unrelated to this
project). Docker is required for the integration tier.

```bash
git clone https://github.com/Abogeerick/fx-engine-take-home.git
cd fx-engine-take-home

# Create venv on Python 3.13. On Windows use `py -3.13`; on POSIX
# use `python3.13` (or whatever your launcher names it).
py -3.13 -m venv .venv                            # Windows
# or:
python3.13 -m venv .venv                          # macOS / Linux

# Activate:
.venv\Scripts\activate                            # Windows (cmd)
source .venv/Scripts/activate                     # Windows (Git Bash)
source .venv/bin/activate                         # macOS / Linux

pip install -e ".[dev]"

# Bring up Postgres for the integration tier.
POSTGRES_PASSWORD=devpass docker compose up -d --wait postgres
```

Then run the tests. `make` is the canonical entry point on
POSIX-like environments; on Windows without `make` installed, use
the raw `python -m` commands shown below.

```bash
# === POSIX (Linux, macOS, or Windows with GNU make installed) ===
make test         # unit + property + integration (120 tests)
make test-unit    # SQLite-tier only (98 tests, no Postgres needed)
make lint         # ruff
make typecheck    # mypy strict on app/domain
make serve        # start uvicorn on :8000
make load-test    # run scripts/load_test.py against :8000

# === Windows (or any environment without make) ===
python -m pytest tests/unit tests/property -p no:unraisableexception
python -m pytest tests/integration -p no:unraisableexception
python -m ruff check .
python -m mypy app/domain
python -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000
python scripts/load_test.py --customers 10 --quotes-per-customer 5
```

Expected output for the full suite: **120 tests pass** (98 in the
unit tier including a 40-example Hypothesis property test; 22 in
the integration tier including the N=20 concurrency test).

The `-p no:unraisableexception` flag is the Windows-specific
platform-noise suppression discussed under "Known limitations"
below; on Linux/macOS it's a no-op but harmless.

## End-to-end demo (two shells)

A linear script that takes a fresh checkout through tests, server,
and a real quote → execute → balance flow. Follow the steps in
order. **Two shells are required** — the server runs in shell 1
and stays running; everything else happens in shell 2. Killing the
server before running the curl flow or the load test surfaces as
`httpx.ConnectError: All connection attempts failed`.

### Shell 2 — verification (run these first)

```bash
# 1. Full test suite — 120 tests across three tiers (~20s)
python -m pytest tests/unit tests/property tests/integration \
  -p no:unraisableexception

# 2. Lint clean
python -m ruff check .

# 3. Type-check strict on the domain layer
python -m mypy app/domain
```

Expected: `120 passed`, `All checks passed!`, `Success: no issues
found in 8 source files`. The full suite covers the SPEC §12 graded
tests (Hypothesis property, N=20 concurrency, M=10 byte-identical
replay, sticky failure, atomicity, stale rate, cross-pair routing,
quote ownership, currency casing).

### Shell 2 — one-time DB setup

```bash
# Apply migrations to the dev database (test DB is handled by
# conftest; dev DB needs this run manually on first checkout).
export DATABASE_URL=postgresql+asyncpg://fx:devpass@localhost:5433/fx_engine
python -m alembic upgrade head

# Seed five mid rates so /quotes works without a real RATE_API_KEY.
python scripts/seed_rates.py
# seeded 5 rates into postgresql+asyncpg://fx:devpass@localhost:5433/fx_engine
```

### Shell 1 — start the server (leave this running)

```bash
export DATABASE_URL=postgresql+asyncpg://fx:devpass@localhost:5433/fx_engine
export ENV=development
python -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000
```

Wait for `Uvicorn running on http://127.0.0.1:8000`. Leave the
window open. Every subsequent step happens in shell 2.

### Shell 2 — quote → execute → balance (the headline demo)

```bash
# 1. Health check
curl -s localhost:8000/healthz | python -m json.tool
# {"status": "ok", "rate_cache_age_seconds": ..., "rate_source_state": "fresh"}

# 2. Create a customer and capture the id
CID=$(curl -s -X POST localhost:8000/customers \
  -H 'content-type: application/json' -d '{}' \
  | python -c "import sys,json;print(json.load(sys.stdin)['customer_id'])")
echo "customer: $CID"

# 3. Fund the customer with 1000 USD
curl -s -X POST "localhost:8000/customers/$CID/credit" \
  -H 'content-type: application/json' \
  -d '{"currency":"USD","amount":"1000"}' | python -m json.tool

# 4. Generate a quote and capture the id
QUOTE=$(curl -s -X POST localhost:8000/quotes \
  -H 'content-type: application/json' \
  -d "{\"customer_id\":\"$CID\",\"from_currency\":\"USD\",\"to_currency\":\"KES\",\"from_amount\":\"100\"}")
echo "$QUOTE" | python -m json.tool
QID=$(echo "$QUOTE" | python -c "import sys,json;print(json.load(sys.stdin)['quote_id'])")

# 5. Execute the quote
curl -s -X POST localhost:8000/executions \
  -H 'content-type: application/json' \
  -d "{\"quote_id\":\"$QID\",\"customer_id\":\"$CID\",\"idempotency_key\":\"demo-001\"}" \
  | python -m json.tool
# 201 with debited / credited / balances_after

# 6. Retry with the same idempotency key — byte-identical replay, HTTP 200
curl -s -X POST localhost:8000/executions \
  -H 'content-type: application/json' \
  -d "{\"quote_id\":\"$QID\",\"customer_id\":\"$CID\",\"idempotency_key\":\"demo-001\"}" \
  | python -m json.tool

# 7. Balance changed exactly once
curl -s "localhost:8000/customers/$CID/balances" | python -m json.tool
# {"USD": "900.00", "KES": "12935.00"}
```

### Shell 2 — load test (optional)

```bash
# Server must still be running in shell 1.
python scripts/load_test.py --customers 10 --quotes-per-customer 5
```

Expected: 50 quote requests + 50 execute requests, 0 errors,
sub-second wall time on a modern laptop.

---

## Running the API locally

First-time setup: apply migrations to the dev database (the tests
use a separate `fx_engine_test` database that conftest creates on
the fly; the dev `fx_engine` needs Alembic run manually).

```bash
# POSIX:
DATABASE_URL=postgresql+asyncpg://fx:devpass@localhost:5433/fx_engine \
  python -m alembic upgrade head

# Windows (cmd):
set DATABASE_URL=postgresql+asyncpg://fx:devpass@localhost:5433/fx_engine
python -m alembic upgrade head
```

Then start the server:

```bash
# POSIX (with make):
DATABASE_URL=postgresql+asyncpg://fx:devpass@localhost:5433/fx_engine \
ENV=development \
make serve

# Windows / no make:
set DATABASE_URL=postgresql+asyncpg://fx:devpass@localhost:5433/fx_engine
set ENV=development
python -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000
```

The rate provider hits exchangeratesapi.io with the `RATE_API_KEY`
env var. The grading flow doesn't need a real key — without one,
`/healthz` reports `degraded` and `/quotes` returns 503 until rates
are seeded. To exercise the API end-to-end without a real key, seed
the rates table from the helper script:

```bash
python scripts/seed_rates.py
# seeded 5 rates into postgresql+asyncpg://fx:devpass@localhost:5433/fx_engine
```

The script writes five mid rates (USD↔{KES,NGN,EUR} plus the EUR
crosses). It honours `DATABASE_URL` if set.

Then in another shell:

```bash
# 1. create a customer
curl -X POST localhost:8000/customers -H 'content-type: application/json' -d '{}'
# {"customer_id":"<uuid>"}

# 2. fund it
curl -X POST "localhost:8000/customers/<uuid>/credit" \
     -H 'content-type: application/json' \
     -d '{"currency":"USD","amount":"1000"}'

# 3. quote
curl -X POST localhost:8000/quotes \
     -H 'content-type: application/json' \
     -d '{"customer_id":"<uuid>","from_currency":"USD","to_currency":"KES","from_amount":"100"}'
# {"quote_id":"<uuid>","from_amount":"100.00","to_amount":"...","rate_applied":"...","routing":"direct",...}

# 4. execute
curl -X POST localhost:8000/executions \
     -H 'content-type: application/json' \
     -d '{"quote_id":"<uuid>","customer_id":"<uuid>","idempotency_key":"first-attempt"}'
# 201 with debited / credited / balances_after
```

`/healthz` reports rate-cache freshness; `/metrics` exposes prometheus
counters (quote count by routing, execute count by status, rate-fetch
latency, idempotent-replay count).

## Load test

`scripts/load_test.py` runs a small concurrent workload against a
running API. Pure asyncio + httpx; no external load tools.

It requires the server to already be running in another shell and
rates to be seeded. Killing the server before launching the load
test surfaces as `httpx.ConnectError: All connection attempts failed`.

```bash
# Shell 1 -- start the server (keep this running):
make serve                                          # POSIX with make
# or:
python -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000

# Shell 2 -- seed rates once, then run the load test:
python scripts/seed_rates.py
make load-test                                      # POSIX with make
# or:
python scripts/load_test.py --customers 10 --quotes-per-customer 5
```

Sample output from a local run captured during the build:

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

Per SPEC §9, every request emits structured JSON with a correlation
ID that threads through every event raised during the request, so a
quote → execute trace can be reconstructed by joining on
`correlation_id`:

```
{"method":"POST","path":"/quotes","status_code":201,"latency_ms":31.7,
 "event":"request.completed",
 "correlation_id":"ede0b1cd-529c-476d-a5e7-2f0e79149365",
 "level":"info","timestamp":"2026-05-08T05:40:02.390214Z"}

{"quote_id":"a1162869-71c1-4034-8d66-fe425b791dbe",
 "execution_id":"5495de55-43ae-4395-a74b-cd61c442a2a5",
 "customer_id":"9d4a0299-72eb-4c69-b0bc-ecdad6016d14",
 "http_status":201,"outcome":"succeeded","is_replay":false,
 "event":"execute.completed",
 "correlation_id":"ede0b1cd-529c-476d-a5e7-2f0e79149365",
 "level":"info","timestamp":"2026-05-08T05:40:02.416425Z"}
```

The two events share `correlation_id`; tracing them together is the
oncall use case.

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
│   └── integration/      Postgres-required tests (FOR UPDATE, N=20, API)
├── alembic/              migrations
├── scripts/
│   └── load_test.py      asyncio + httpx load script
├── docs/
│   ├── SPEC.md           technical specification (v0.2)
│   ├── DECISIONS.md      one-page submission summary
│   ├── DECISIONS_LOG.md  full running log of step-by-step decisions
│   └── REVIEW.md         planted-bugs review
└── planted_bugs/         the assignment-provided code under review (read-only)
```

## How the build was structured

Eleven commits across seven explicit steps, each scoped and reviewed
before the next:

```
1be129a chore: import assignment artifacts as received     (step 0)
1f6790b chore: add project-level gitignore
f710345 docs: add technical specification                  (SPEC v0.1)
ecd43b7 docs: add agent operating instructions             (CLAUDE.md)
037f6c8 spec: clarify ambiguities surfaced by agent read-back  (SPEC v0.2)
870ef3e feat(domain): add Money, Currency, Clock primitives    (step 1)
9b94770 feat(infra): add Alembic + schema for customers, balances, rates  (step 2)
554a664 feat(core): add quotes, executions, ledger + atomic execute       (step 3)
fcd3670 feat(rates): add rate provider with circuit breaker and singleflight  (step 4)
7147f62 feat(api): add HTTP routes, observability, and graded tests       (step 5)
87a54e6 review: add planted_bugs review (REVIEW.md)        (step 6)
```

The narrative is reviewable end-to-end via the commit messages.

## Known limitations

- **Rate API key:** the engine's tests pass without a real
  `RATE_API_KEY` because they fake the rate source. A live
  deployment needs a paid exchangeratesapi.io key; the free tier
  locks `base` to EUR.
- **Singleflight scope is in-process** (per SPEC §8). In a
  multi-worker deployment, up to N upstream calls per refresh
  window can occur. Production would use a DB advisory lock or
  Redis SETNX for global singleflight. Documented in DECISIONS.md.
- **Cross-table FK** from `quotes.consumed_by_execution_id` to
  `executions.id` is enforced only at the ORM layer, not in the
  DB. SQLite cannot `ALTER TABLE ADD CONSTRAINT FOREIGN KEY` on
  existing tables, and the `quotes` table is created before
  `executions`. The integrity argument is that the only writer is
  the execute orchestrator, in the same transaction as the
  executions insert.
- **Step ordering deviation from SPEC §7** in the execute
  orchestrator (lock quote BEFORE inserting executions row). The
  literal SPEC ordering deadlocks under N parallel executes on
  Postgres. All observable SPEC behaviours preserved; documented
  inline in `app/services/execute.py` and in DECISIONS.md.
- **Strict-warnings policy on Windows.** The strict
  `filterwarnings = ["error"]` regime would otherwise escalate
  `PytestUnraisableExceptionWarning`s emitted by asyncio proactor
  / asyncpg / aiosqlite cleanup paths during garbage collection,
  attributed non-deterministically to whichever test was running.
  The unraisable-exception hook is disabled (`-p
  no:unraisableexception` on both `make test-unit` and `make
  test-integration`) and a single `ignore::pytest.PytestUnraisable
  ExceptionWarning` is added to `filterwarnings`. The strict regime
  remains live for warnings raised inside test code. On Linux and
  macOS the cleanup paths are synchronous and the suppression is a
  no-op. See `tests/integration/conftest.py` for the full
  rationale.

## What I'd do with another day

- **Per-pair, per-tier spreads.** SPEC §13 calls this out as a
  real fintech requirement; the code is structured for it (the
  pricing service is cleanly separated) but the current spread is
  a single `Decimal` config value.
- **Global singleflight.** A DB advisory lock or Redis SETNX would
  reduce upstream fan-out under multi-worker deployments to 1 per
  refresh window.
- **Property test on the cross-pair compounding identity.** The
  current Hypothesis test asserts post-trade balances match the
  rounded amounts. A separate property could assert that the
  computed cross rate equals `leg1 × leg2 × (1 - s)²` to within
  Decimal precision for any randomised pair of mid-rates.
- **Failed-execution row TTL.** Sticky failures accumulate
  forever. A real deployment would TTL them after some retention
  window or move them to cold storage.

## Estimated time

- **Wall clock:** Wednesday afternoon through Sunday morning.
- **Active engagement:** ~14–16 hours, broken across seven named
  build steps. Each step's commit body shows the scope; each
  step's CLAUDE.md-driven workflow (restate goal → list files →
  tests first → implement → verify → diff → commit) shows the
  cadence.

## Notes to the reviewer

A few things worth surfacing explicitly (the assignment's
"anything you want us to know" line).

- **I treated this as a process exam dressed as a coding exam.**
  The order matters and is visible in the git log: SPEC.md was
  committed *before* CLAUDE.md (the agent operating instructions),
  which was committed *before* any implementation code. After the
  agent's pre-implementation read-back surfaced twelve clarifications,
  SPEC v0.2 landed as its own commit. Implementation followed in
  scoped, reviewable steps with explicit acceptance criteria.
- **One technical decision worth scrutinising is the SPEC §7
  step-ordering deviation** in `app/services/execute.py`. The
  orchestrator's docstring restates the spec step-for-step and
  explains why this implementation locks the quote *before*
  inserting the executions row, contrary to the spec's literal
  order. The N=20 concurrency test on Postgres surfaced the
  deadlock the literal ordering would have produced; the
  FK-vs-FOR-UPDATE reasoning is documented inline and in
  DECISIONS.md.
- **Strict tooling caught four real bugs during the build.**
  Listed in DECISIONS.md under "What I did not trust without
  verifying." That thread is what I would point at if asked how
  I work with AI tools.
- **The planted-bugs review identifies nine bugs ranked by
  production impact.** The most severe is a cross-pair routing
  inversion that produces ~17,000× the correct rate; it survives
  the planted suite's own tests because the suite uses a
  MagicMock provider and only asserts `rate > 0`. Captured in
  REVIEW.md.
- **One process slip surfaced honestly.** During the planted-bugs
  review I `cd`'d into `planted_bugs/` and inadvertently modified
  its `.gitignore` (a CLAUDE.md §7 read-only-rule violation).
  Caught within the minute, reverted with `git checkout`, no
  commit pollution. Surfaced in DECISIONS.md because process-
  honesty is the discipline being graded.
- **Strict-warnings policy was scope-corrected during step 7.** I
  argued for keeping strict on the unit tier because it had
  caught a real bug; the agent re-examined and found the bug was
  caught by the same hook I was about to disable. Reframed and
  widened the suppression scope. Preserved in DECISIONS.md
  because reframes-on-deeper-examination are the engineering
  signal worth preserving, not a polished victory narrative.
