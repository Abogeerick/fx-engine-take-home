SPEC — Umba FX Engine
Status: v0.2 — clarifications from pre-implementation read-back.
Author: Erick Aboge
Scope: Take-home assignment. Production-ready in approach, time-boxed in surface area.
Changelog:

v0.2: Quote ownership on execute now explicit; rate convention stated;
idempotency replay semantics for failures stated; payload-difference
scope narrowed to quote_id; status names unified across §6 and §8;
currency code casing rule added; admin endpoint gating clarified;
test DB split made explicit; cross-pair staleness precedence stated;
credit fixture validation tightened; spread compounding reworded.
v0.1: initial draft.


1. Purpose
A foreign-exchange engine that issues short-lived quotes between a fixed set
of currencies and atomically executes those quotes against per-customer
multi-currency balances. The engine is the system of record for both quotes
and balances. External rates are a dependency, not the source of truth for
executed trades.
2. Currencies and pairs
Supported currencies: USD, EUR, KES, NGN. Currency codes are
uppercase ISO 4217 strings. Lowercase or mixed-case input is
rejected with HTTP 400 — codes are not silently normalized, because
silent normalization hides client bugs.
Direct pairs (mid-rates fetched from the rate source): USD/KES, USD/NGN,
USD/EUR, EUR/KES, EUR/NGN. Inverses are derived (1 / direct).
Cross pairs (no direct mid-rate): KES/NGN and NGN/KES.
Routing rule for cross pairs: route through USD by default, EUR as
fallback if either USD leg is unusable. The cross rate is the product of
the two leg rates, with spreads applied to each leg independently and
then compounded — see §5.
Rate convention: for a row in rates keyed (base_currency, quote_currency), mid_rate is quote per base — i.e. for
(USD, KES) with mid_rate = 130.00, one USD is worth 130 KES at
mid-market. This convention matches the upstream API and standard FX
display.
Minor units (decimal places at API boundary):
CurrencyMinor unitsUSD2EUR2KES2NGN2
Internal precision: all computation in Decimal with 8 fractional
digits. Rounding to minor units happens only at API response
serialization and balance display — never mid-computation.
3. Rounding

Rounding mode: ROUND_HALF_EVEN (banker's rounding) everywhere.
Rationale: unbiased over many transactions; matches IEEE 754 default
and standard financial-systems convention.
Decimal context: precision 28, rounding ROUND_HALF_EVEN. Set once
at process start.
The displayed final_amount on a quote is the rounded value the
customer will receive. The internal ledger entries use the same
rounded values — i.e. what we quote is what we book. There is no
internal "true" amount diverging from the displayed amount post-quote.

4. Data model
customers

id (uuid, pk)
created_at

balances

customer_id (fk)
currency (text, one of {USD, EUR, KES, NGN})
amount (NUMERIC(20, 8), non-negative invariant)
pk: (customer_id, currency)

rates

base_currency, quote_currency (text)
mid_rate (NUMERIC(20, 8), interpreted as quote-per-base — see §2)
fetched_at (timestamptz)
source (text)
pk: (base_currency, quote_currency) — one row per direct pair, upserted
on each refresh.

quotes

id (uuid, pk)
customer_id (fk)
from_currency, to_currency
from_amount (NUMERIC(20, 8))
to_amount (NUMERIC(20, 8))
rate_applied (NUMERIC(20, 8)) — the effective rate including spread
routing (text) — "direct", "via_USD", or "via_EUR"
created_at, expires_at
consumed_at (nullable timestamptz)
consumed_by_execution_id (nullable fk)

executions

id (uuid, pk)
quote_id (fk)
customer_id (fk)
idempotency_key (text)
status (text: "succeeded" | "failed")
failure_reason (nullable text)
response_body (jsonb) — the exact response returned to the client,
used to replay verbatim on idempotent retries
created_at
unique constraint: (customer_id, idempotency_key)
partial unique index on quote_id WHERE status = 'succeeded' —
a quote may have multiple failed execution rows (across different
keys) but at most one successful one.

ledger_entries

id (uuid, pk)
execution_id (fk)
customer_id (fk)
currency
amount (NUMERIC(20, 8), signed: negative for debit, positive for credit)
created_at
Two rows per successful execution: one debit on the from currency,
one credit on the to currency. The ledger is append-only; balances
are derived from it but materialized in balances for fast reads.

5. Spread model

Spread is a fixed percentage s applied symmetrically around the mid-rate.
Default: s = 0.5% (0.005), configurable via env var.
For a quote selling from_currency for to_currency:

If the pair is direct, effective rate = mid * (1 - s).
If the pair is the inverse of a direct pair, effective rate
= (1 / mid) * (1 - s).


For cross pairs routed through a hub currency H:

Compute leg 1 (from -> H) at its effective rate.
Compute leg 2 (H -> to) at its effective rate.
Final rate = leg1_rate × leg2_rate.
Spread compounds. With s = 0.5% per leg, the end-to-end customer
cost is 1 - (1 - s)^2 = 1 - 0.990025 = 0.009975, i.e. roughly
1.0% total. This is the documented behaviour, not a defect:
customer-facing materials would describe it; this engine does not
hide it.


The rate_applied field on the quote records the final effective rate
the customer is being shown.

6. Endpoints (HTTP / FastAPI)
All responses include X-Correlation-ID echoed from the request or
generated if absent.
POST /quotes
Request:
{
  "customer_id": "uuid",
  "from_currency": "USD",
  "to_currency": "KES",
  "from_amount": "100.00"
}
Response 201:
{
  "quote_id": "uuid",
  "from_currency": "USD",
  "to_currency": "KES",
  "from_amount": "100.00",
  "to_amount": "12967.50",
  "rate_applied": "129.675",
  "routing": "direct",
  "expires_at": "2026-05-07T14:30:00Z"
}
Errors:

400 invalid currency (including non-uppercase codes), non-positive
amount, same from/to.
503 if the rate source is unavailable and the cached rate for any
required leg is stale_unusable (see §8).

POST /executions
Request:
{
  "quote_id": "uuid",
  "customer_id": "uuid",
  "idempotency_key": "client-generated-string"
}
Headers: Idempotency-Key accepted as alternative to body field; body
field takes precedence if both present.
Quote ownership: the request-supplied customer_id MUST match
quote.customer_id. If it does not, the endpoint returns 404 (not 403)
to avoid leaking the existence of quotes belonging to other customers.
Response 201 (first execution, success):
{
  "execution_id": "uuid",
  "quote_id": "uuid",
  "status": "succeeded",
  "debited":  { "currency": "USD", "amount": "100.00" },
  "credited": { "currency": "KES", "amount": "12967.50" },
  "balances_after": {
    "USD": "900.00",
    "KES": "12967.50"
  }
}
Response 200 (idempotent replay): identical body to the original
response, replayed verbatim from executions.response_body. Applies
regardless of whether the original execution succeeded or failed — see
§10 on sticky failures.
Errors:

404 quote not found, or quote belongs to a different customer.
409 the same idempotency key was previously used with a different
quote_id, OR the quote has already been successfully consumed by
a different idempotency key.
410 quote expired.
422 insufficient balance on from currency.
400 idempotency key missing.

GET /customers/{id}/balances
Returns balances per currency, rounded to minor units.
POST /customers (test fixture)
Creates a customer with zero balances across all four currencies.
Disabled when ENV=production.
POST /customers/{id}/credit (test fixture)
Manually credit a balance. Amount must be strictly positive — zero or
negative requests are rejected with 400, to prevent the fixture from
violating the non-negative balance invariant. Disabled when
ENV=production.
POST /admin/rates/refresh
Force-refresh the rate cache. In ENV=production this requires a
shared-secret header X-Admin-Token matching ADMIN_TOKEN env. In
non-production environments the endpoint is open. Used in tests and
ops.
GET /healthz
Returns 200 with { "status": "ok", "rate_cache_age_seconds": N, "rate_source_state": "fresh" | "cached" | "stale_unusable" }. Status
names are aligned with §8.
GET /metrics
Prometheus-format metrics: quote count, execute count by status,
rate-fetch latency, rate-fetch failure count, idempotent-replay count.
7. Concurrency model
Locking: pessimistic, via SELECT ... FOR UPDATE on the two relevant
balance rows inside the execute transaction.
Lock order: balance rows are locked in alphabetical order by currency
code to prevent deadlocks when two simultaneous executes touch the same
two currencies in opposite directions.
Execute transaction sequence:

Begin transaction.
Insert into executions with (customer_id, idempotency_key). The
unique constraint detects replays; on conflict, read the existing
row and return its response_body with HTTP 200, regardless of
whether the original succeeded or failed.
SELECT ... FOR UPDATE on the quote row. Validate:

quote.customer_id == request.customer_id (else 404)
consumed_at IS NULL
expires_at > now()


SELECT ... FOR UPDATE on the two balance rows in lock order.
Validate from balance >= from_amount.
Update balances (debit + credit).
Insert two ledger entries.
Update quote: set consumed_at, consumed_by_execution_id.
Persist response_body and status='succeeded' on the executions row.
Commit.

Business-logic failures (insufficient balance, expired quote, ownership
mismatch) commit a status='failed' executions row with the failure
response in response_body, so the failure is sticky on the
idempotency key — see §10.
DB-level failures (transient, e.g. lock timeout, connection drop) roll
back the entire transaction including the executions row, leaving the
idempotency key available for retry.
Quote expiration TOCTOU: expiration is checked inside the
transaction at step 3, not before it. A quote that expires between the
client's quote-fetch and execute call will fail at step 3 with HTTP 410.
Idempotency conflict resolution:

Same (customer_id, idempotency_key), same quote_id: replay the
stored response with HTTP 200.
Same (customer_id, idempotency_key), different quote_id: HTTP 409.
The server compares the request's quote_id against the existing
executions row's quote_id; this is narrower than "any request field
differs" and is the only collision the engine detects.

8. Rate source policy
Source: exchangeratesapi.io (free tier) for live mid-rates,
interpreted as quote-per-base per §2.
Refresh cadence: background task every 60 seconds. Rates are also
refreshed on demand if the cache is older than 60 seconds when a quote
is requested.
Cache: in-database (rates table), survives process restarts.
Staleness tiers:

fresh — fetched_at within last 60s. Quote freely.
cached — fetched_at between 60s and 10 minutes. Quote freely; log
a rate.cache.served event.
stale_unusable — fetched_at older than 10 minutes. Refuse to
quote with HTTP 503. Health check reports degraded.

Cross-pair staleness precedence: for a cross-pair quote (KES/NGN or
NGN/KES), evaluate routing options in order:

Try USD route. If both legs (from/USD and USD/to) are not
stale_unusable, use it.
Else try EUR route. If both legs are not stale_unusable, use it.
Else return HTTP 503.

The choice is 503 not 422 because the failure is server-side: the rate
cache is stale; the client request is well-formed.
Rationale for the 10-minute threshold: a fintech that quotes on
stale rate data eats the spread when the market moved. Failing closed
is the conservative call. The threshold is configurable and would be
tuned per pair volatility in production.
Circuit breaker on the upstream HTTP call:

After 3 consecutive failures, the breaker opens for 30 seconds.
Open state: skip the HTTP call entirely, fall through to the cache.
Half-open after the cooldown: one trial request; success closes the
breaker, failure re-opens it.

Race against the freshness window: if two requests arrive
simultaneously when the cache is at 59.9s, only one triggers a refresh.
The second waits up to 5 seconds for the in-flight refresh to complete,
then reads the new value. A singleflight-style coalescer guards the
fetch.
Singleflight scope: the coalescer is in-process. In a multi-worker
deployment, up to N upstream calls per refresh window can occur. This
is accepted for the take-home; production would use a DB advisory lock
or Redis SETNX for global singleflight.
9. Observability

Correlation IDs. Every request gets one (from X-Correlation-ID
header or generated). Quote IDs and execution IDs are logged with the
correlation ID, so a quote → execute trace can be reconstructed from
logs.
Structured logs (JSON). Every domain event emits one log line with
fields: event, correlation_id, customer_id, quote_id,
execution_id (where relevant), currency_pair, amount,
latency_ms, outcome.
Metrics: as listed in §6 under /metrics.
Health: /healthz reports rate-cache age and source state.

10. Failure modes and responses
FailureResponsePersistent state change?Rate source down, all required legs fresh or cachedQuote succeedsCache hit loggedRate source down, any required leg stale_unusable503NoneQuote expired before execute410Failed execution row writtenDuplicate idempotency key, same quote_id200 with original responseNoneDuplicate idempotency key, different quote_id409NoneInsufficient balance422Failed execution row writtenProcess killed mid-execute (between debit and credit)N/A — single transactionNone — DB rollbackTwo parallel executes on same quote, same keyOne 201, other replays 200One execute persistsTwo parallel executes on same quote, different keysOne 201, other 409One execute persistsQuote belongs to a different customer404NoneCurrency code in lowercase400None
Sticky failures. Once an execution is recorded with
status='failed' for a given (customer_id, idempotency_key),
subsequent requests with the same key replay that failure response —
they do not get a fresh attempt. Reason: idempotency keys exist to
make transport retries safe; semantic retries are the client's
responsibility and require a new key. This matches Stripe and similar
APIs.
11. Out of scope
Explicitly not built or addressed:

Auth / authz (any customer_id is accepted).
Multi-quote batch execution.
Partial fills.
Limit orders, stop orders, anything order-book-shaped.
Settlement, netting, T+N delivery.
FX hedging or P&L tracking.
Regulatory reporting (CTR, SAR, etc.).
Multi-region replication, read replicas.
Rate-source redundancy (multiple providers with arbitration).
Customer-facing rate transparency UI.
KYC, sanctions screening.
Global singleflight across workers (see §8).

12. Testing requirements (graded)
Test database split:

Unit and property tests run against SQLite for speed.
Concurrency, integration, and atomicity tests require Postgres
(SQLite serializes writers at the file level and cannot exercise the
row-locking model).

Tests that must exist and pass for the assignment to be considered
complete:

Decimal property tests (Hypothesis): for random valid amounts and
random pairs, the round-trip quote → execute → balance invariant
holds: post-balances reflect the rounded from_amount and to_amount
exactly; no fractional drift.
Concurrency test (Postgres): N=20 parallel executes of the same
quote with distinct idempotency keys. Exactly one returns 201; the
rest return 409. Final balance reflects exactly one execution.
Idempotency test: same (customer_id, idempotency_key) retried
M=10 times. First returns 201, rest return 200 with byte-identical
bodies. Balance changes once.
Sticky-failure test: first attempt fails (e.g. insufficient
balance, 422); a retry with the same idempotency key returns the
stored 422 response, not a fresh evaluation.
Atomicity test (Postgres): simulate a credit-leg failure (via a
test-only hook gated by env var) and assert the debit was rolled
back. Balance unchanged.
Stale-rate test: advance clock past 10-minute threshold with rate
source mocked-down; assert 503 on quote.
Cross-pair routing test: quote KES → NGN; assert routing field
is via_USD; assert effective rate equals product of leg rates with
compounded spread. Additional cases: USD route partially stale →
via_EUR; both routes stale → 503.
Insufficient balance test: customer with $50 USD, quote of $100
USD → KES; execute returns 422 and the quote is not marked consumed.
Quote-ownership test: customer A creates a quote; customer B
tries to execute it; returns 404 not 403.
Currency casing test: lowercase currency code on /quotes
returns 400.

A scripts/load_test.py runs a small concurrent workload against
Postgres and prints results — used as evidence that the concurrency
story holds beyond a single test case.
13. Open questions / assumptions

No customer creation API in production. Real Umba would not let
an FX engine create customers; that's an upstream concern. The
fixture endpoint exists for testability and is config-gated.
Spread is global, not per-pair. A real fintech would have
per-pair, per-tier spreads. The spec uses a single spread for
simplicity; the code structures it as a per-pair lookup with a
default to make extension trivial.
No multi-currency aggregation in /balances — returns balances
per currency without conversion to a base. Conversion would be
lossy and stale and isn't asked for.