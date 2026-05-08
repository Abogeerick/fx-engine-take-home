# Code Review — `planted_bugs/`

**Reviewer:** Erick Aboge
**Tools used:**
- Claude Code (this assistant) for inspection, hypothesis formation, and
  drafting reproductions.
- Python interactive shell + a scratch directory of single-purpose
  reproduction scripts (one per hypothesis, not committed) to confirm
  every claimed bug fires before promoting it to this document.
- The planted suite's own pytest run, to confirm the baseline claim that
  it passes.

**Approach:** I read every file in `planted_bugs/` line by line before
writing anything down. I then ran the existing pytest suite (8 tests,
all green — the bugs survive their own coverage). I formed nine
hypotheses, wrote each as a one-sentence claim before verifying, and
reproduced each in a `scratch/` script. Items I considered and rejected
as non-bugs are listed at the end.

The list is ranked by **production impact**, not by code-smell weight or
how clever the bug is.

---

## Bugs

### 1. Cross-pair routing produces a ~17,000× rate (BLOCKER)

**Severity:** Blocker
**Where:** `planted_bugs/fx.py` lines 192–200

```python
# Cross via USD.
leg1 = self.rates.get(f"{from_ccy}/USD") or self.rates.get(
    f"USD/{from_ccy}"
)
leg2 = self.rates.get(f"USD/{to_ccy}") or self.rates.get(
    f"{to_ccy}/USD"
)
if leg1 and leg2:
    return leg1["sell"] * leg2["sell"]
```

**What's wrong:** When the first lookup `{from_ccy}/USD` misses and the
fallback `USD/{from_ccy}` hits, the code uses that direct rate as
though it were the inverse. The product is dimensionally wrong — for
`KES → NGN` it computes `(KES/USD) × (NGN/USD)` and treats the result
as `NGN/KES`. The fallback also has no spread inversion logic.

**Reproduction:**
```
KES -> NGN, amount=100
  rate stored: 193581.3915000000
  final_amount: 19358139.15  NGN
  expected:    ~1140 NGN
```
A customer who deposits 100 KES (about US$0.77) walks out with 19.3
million NGN (about US$13,000). In the other direction, the customer
gets effectively zero. The bug is symmetric and catastrophic in either
direction.

**Why it matters in production:** A single accepted KES→NGN trade
drains operating funds. SPEC §2 lists `KES/NGN` and `NGN/KES` as the
two cross pairs the engine must handle; the engine handles them by
silently producing wildly wrong amounts and writing them to the
ledger.

**Fix:** When falling back to the inverse leg, invert it before
multiplying:
```python
leg1_rate = leg1["sell"] if direct_hit_1 else (Decimal(1) / leg1["sell"])
leg2_rate = leg2["sell"] if direct_hit_2 else (Decimal(1) / leg2["sell"])
return leg1_rate * leg2_rate
```
And apply the customer-unfavourable side per SPEC §5 (see Bug #4).

**SPEC reference:** §2 (cross pair routing); §5 (rate composition).

---

### 2. Spread is applied in the customer-favourable direction (BLOCKER)

**Severity:** Blocker
**Where:** `planted_bugs/fx.py` lines 183–190 (direct + inverse paths)

```python
direct = self.rates.get(f"{from_ccy}/{to_ccy}")
if direct is not None:
    return direct["sell"]               # mid * (1 + s)  -- customer-favourable

inverse = self.rates.get(f"{to_ccy}/{from_ccy}")
if inverse is not None:
    mid = (inverse["buy"] + inverse["sell"]) / 2
    return Decimal("1") / mid           # 1/mid  -- no spread at all
```

**What's wrong:** SPEC §5 prescribes `rate = mid × (1 − s)` for direct
trades and `(1 / mid) × (1 − s)` for inverses. Both use the
*customer-unfavourable* direction so the bank earns the spread. The
planted code uses `direct["sell"] = mid × (1 + s)` for the direct
branch (customer gets *more* than mid) and `1 / mid` for the inverse
(no spread at all). The bank pays the spread on every trade.

**Reproduction:** A 100 USD round-trip through KES:
```
USD/KES rates: buy=128.85250, sell=130.14750
USD->KES effective rate: 130.14750
KES->USD effective rate: 0.007722007722  (= 1 / 129.5 mid)

100 USD -> 13014.75 KES -> 100.50 USD
  bank earned: -0.50 USD
  expected (both legs spread-adjusted): 99.0025 USD
  bank revenue gap (over-credit): 1.4975 USD per 100-USD round trip
```

**Why it matters in production:** The bank pays customers to trade
through it. At Umba's volumes, this is a continuous P&L leak in the
direction opposite to what FX engines exist to capture. A customer
running a tight automated round-trip loop monetises the bug directly.

**Fix:**
```python
if direct is not None:
    return direct["buy"]                 # mid * (1 - s); SPEC §5
if inverse is not None:
    return Decimal(1) / inverse["sell"]  # equivalent to (1/mid) * (1-s)
```

**SPEC reference:** §5 spread model.

---

### 3. TOCTOU race lets one quote execute N times (BLOCKER)

**Severity:** Blocker
**Where:** `planted_bugs/fx.py` lines 121–139

```python
now = datetime.now(timezone.utc)
expires_at = datetime.fromisoformat(row["expires_at"])
if expires_at < now:
    raise ValueError("quote expired")
if row["executed"]:                     # <-- check is OUTSIDE the lock
    raise ValueError("quote already executed")

current_rate = self._effective_rate(...)
amount = Decimal(row["amount"])
final = (amount * current_rate).quantize(...)

with _execute_lock:                     # <-- lock acquired AFTER check
    conn.execute(
        "UPDATE quotes SET executed = 1, executed_at = ? WHERE id = ?",
        ...
    )
    ...
    conn.execute("INSERT INTO transactions ...")
```

**What's wrong:** The `executed` check happens before the lock. Twenty
threads can all read `executed = 0`, all serialise on the threading
lock one at a time, and all proceed to `UPDATE executed = 1` (a no-op
on rows already set) and `INSERT INTO transactions`. The result: N
transaction rows for one quote. Worse, `threading.Lock` is per-process
— multi-worker deployments lose even this incomplete protection, and
SQLite has no row-level locking to backstop it.

**Reproduction:** 20 threads, single quote:
```
=== 20 threads, same quote_id ===
  succeeded:   11
  failed:      9
  transactions in DB: 11
```
Eleven transaction rows for one quote. In an account-balance world,
the customer is debited eleven times.

**Why it matters in production:** This is the classic "double charge"
incident, except eleven-fold. Worth note: it survives under the
existing test suite because none of their tests exercise concurrent
execute. SPEC §12 makes this category of test mandatory for that
exact reason.

**Fix:** Drop the threading lock. Use DB-level locking. On Postgres,
`SELECT ... FOR UPDATE` on the quote row inside a single transaction
serialises N concurrent executes through the row's exclusive lock
without false-success on retries. SQLite's database-level write
serialisation covers single-instance dev. A unique constraint on
something like `transactions (quote_id) WHERE status='succeeded'`
turns any leaked race into a loud `IntegrityError`.

**SPEC reference:** §7 concurrency model; §12 graded N=20 test.

---

### 4. `execute_quote` recomputes the rate; the quote is not honoured (BLOCKER)

**Severity:** Blocker
**Where:** `planted_bugs/fx.py` line 126

```python
current_rate = self._effective_rate(
    row["from_currency"], row["to_currency"]
)
amount = Decimal(row["amount"])
final = (amount * current_rate).quantize(QUANTUM, rounding=ROUND_HALF_UP)
```

**What's wrong:** `current_rate` is computed at execute time. The
stored `row["rate"]` and `row["final_amount"]` from the quote are
ignored. If rates move within the 60-second TTL — which is exactly
what TTLs are for — the customer is charged whatever the new rate
says, not what they accepted.

**Reproduction:** Quote at the seed rate, simulate an upstream rate
spike, execute:
```
=== generate_quote ===
  rate stored on quote: 130.14750
  final on quote:       13014.75 KES

=== execute_quote (after upstream rate moves to 999.99) ===
  rate on result:       999.99
  final_amount:         99999.00 KES
```
Customer accepted a 100 USD → 13,014.75 KES quote. They got 99,999 KES.
On a real downward rate move, the customer would be silently shorted
in the opposite direction.

**Why it matters in production:** This breaks the entire premise of a
quote. SPEC §3 says explicitly: "what we quote is what we book." This
is the contract a customer relies on when they hit "confirm." Real
fintech regulators care about this exact behaviour because it crosses
into unauthorised pricing. Dispute-resolution costs follow.

**Fix:** Use the rate stored on the quote row at execute time. The
`row["rate"]` and `row["final_amount"]` are the contract; recomputing
breaks the contract.

**SPEC reference:** §3 ("what we quote is what we book").

---

### 5. Idempotency replay returns the cached response without checking quote_id (MAJOR)

**Severity:** Major
**Where:** `planted_bugs/fx.py` lines 102–110

```python
if idempotency_key:
    with get_db() as conn:
        row = conn.execute(
            "SELECT response FROM idempotency WHERE key = ?",
            (idempotency_key,),
        ).fetchone()
        if row:
            import json
            return json.loads(row["response"])
```

**What's wrong:** The replay path looks up the cached response by
idempotency key alone. If a client reuses the same key for a
*different* quote, the cached response from the first quote is
returned to the second call. SPEC §10 mandates HTTP 409 for the
"same key, different payload" case.

**Reproduction:**
```
quote A: USD->KES 100
quote B: USD->EUR 250

execute A with key 'reused' -> {"quote_id": <A>, "to_currency": "KES", "final_amount": "13014.75"}
execute B with key 'reused' -> {"quote_id": <A>, "to_currency": "KES", "final_amount": "13014.75"}
                                            ^^^                ^^^                       ^^^^^^^^
                              client thought it executed B (USD->EUR), got A's response
```

**Why it matters in production:** Clients reuse idempotency keys for
all sorts of reasons (bugs, key generators with low entropy, retry
logic that doesn't regenerate). The quiet-mismatch failure mode is
worse than an explicit error — the client thinks it executed B and
moves on, while the server hasn't actually executed B and never
will.

**Fix:** Store the quote_id alongside the cached response (or include
it in the response key). On replay, compare `cached.quote_id` against
the requested `quote_id`; if they differ, return 409. Stripe's
idempotency model is the canonical reference here.

**SPEC reference:** §6 (409 on idempotency-key reuse with different
payload); §10 failure-mode table.

---

### 6. All execute failures return HTTP 400 (MAJOR)

**Severity:** Major
**Where:** `planted_bugs/app.py` lines 67–72

```python
try:
    result = engine.execute_quote(quote_id, idempotency_key=...)
except ValueError as e:
    return jsonify({"error": str(e)}), 400
```

**What's wrong:** Every execute failure bubbles up as `ValueError`
and the route flattens all of them to HTTP 400. SPEC §6 / §10 require
differentiated codes:
- quote not found        → 404
- quote expired          → 410
- quote already executed → 409
- insufficient balance   → 422
- bad request            → 400

**Reproduction:**
```
quote not found:        HTTP 400  body={'error': 'quote not found'}
quote expired:          HTTP 400  body={'error': 'quote expired'}
quote already executed: HTTP 400  body={'error': 'quote already executed'}
```

**Why it matters in production:** Client retry/give-up logic depends
on these codes. A 410 means "stop retrying, the quote is dead;
re-quote." A 409 means "stop retrying, this quote already executed;
fetch the result by id." Collapsing them all to 400 forces the client
to scrape the error string and parse it — fragile, and a contract
that's not documented anywhere stable.

**Fix:** Map the domain failures to typed exceptions
(`QuoteNotFound`, `QuoteExpired`, `QuoteAlreadyConsumed`, etc.),
register a single error-handler table that maps each to its SPEC
status code, and stop using bare `ValueError` for control flow at
the route boundary.

**SPEC reference:** §6 endpoint error responses; §10 failure-mode table.

---

### 7. Float arithmetic in `generate_quote` (MAJOR)

**Severity:** Major
**Where:** `planted_bugs/fx.py` lines 60–63

```python
final = float(amount) * float(rate)
final_decimal = Decimal(str(final)).quantize(
    QUANTUM, rounding=ROUND_HALF_UP
)
```

**What's wrong:** SPEC §3 states explicitly: "no floats for money".
The code does float multiplication, then round-trips through `str` to
recover a `Decimal`. The `str(float)` shortest-repr algorithm hides
the precision loss for small typical values, but it's a real
violation that bites at scale and in edge cases.

A second, related defect is the *asymmetry* with `execute_quote`
(line 130) which uses pure-Decimal arithmetic. The two compute
slightly different `final_amount` values for the same inputs — and
the planted code doesn't even use the quoted `final_amount` at execute
time (Bug #4 above), it recomputes via Decimal. So the customer sees
the float-derived value in the quote response and gets a different
Decimal-derived value at execute, even when the rate doesn't move.

**Reproduction:** The float divergence is visible at large amounts:
```
amount=1234567890123456, rate=1.0001:
  pure Decimal:  1234691346912468.35
  via float:     1234691346912468.20
  divergence:    0.15
```
For typical Umba amounts the 2-dp rounding masks the divergence on a
single trade. Across millions of trades, the asymmetry shows up in
reconciliation.

**Why it matters in production:** Financial systems are graded on
deterministic reproducibility. The float path is non-deterministic
across CPU architectures (rare in practice, but real) and hides bugs
that surface during settlement reconciliation rather than at the
trade itself. Auditors reject this category of code.

**Fix:** Drop the floats:
```python
final_decimal = (amount * rate).quantize(
    QUANTUM, rounding=ROUND_HALF_UP
)
```
And tie this to Bug #4: store the rounded value on the quote and
reuse it at execute, instead of recomputing at execute time.

**SPEC reference:** §3 hard rule (no floats for money); §3 invariant
("what we quote is what we book").

---

### 8. `X-Correlation-ID` is not echoed; middleware reads the wrong header (MAJOR)

**Severity:** Major
**Where:** `planted_bugs/app.py` lines 26–30

```python
@app.before_request
def _attach_correlation_id():
    request.environ["correlation_id"] = (
        request.headers.get("X-Request-Id") or str(uuid.uuid4())
    )
```

**What's wrong:** Two related defects.
1. The middleware reads `X-Request-Id`. SPEC §6 mandates
   `X-Correlation-ID`. If a client sends the spec-conforming header,
   it is silently ignored and a fresh UUID is generated server-side.
2. The middleware never writes the correlation id back to the
   response. SPEC §6: "All responses include `X-Correlation-ID`
   echoed from the request or generated if absent."

In addition, the route handlers' explicit `log.info(...)` calls
(line 57, line 74) don't include the correlation_id, so even
server-side log correlation is broken for normal-path requests.

**Reproduction:**
```
request had X-Correlation-ID: test-cid-12345
response X-Correlation-ID:    (missing)
response X-Request-Id:        (missing)
```

**Why it matters in production:** Audit trails are a regulatory
concern in fintech. Without correlation IDs threading from the
client through to server logs, you cannot reconstruct what a customer
did during a dispute. Distributed tracing across services (Umba's
mobile app → API → ledger → notification) loses its primary join key.

**Fix:** Read `X-Correlation-ID`, write it to the response header,
and bind it to the logging context (e.g. via `structlog.contextvars`
or Flask's `g`) so every `log.info` call within the request scope
auto-includes it.

**SPEC reference:** §6 (echo header); §9 (correlation ID in log fields).

---

### 9. Currency codes are silently uppercased (MINOR)

**Severity:** Minor
**Where:** `planted_bugs/app.py` lines 46–47

```python
from_ccy = data["from_currency"].upper()
to_ccy = data["to_currency"].upper()
```

**What's wrong:** SPEC §2 states explicitly: "Lowercase or mixed-case
input is rejected with HTTP 400 — codes are not silently normalized,
because silent normalization hides client bugs." The planted code
silently uppercases.

**Reproduction:**
```
POST /quotes {"from_currency": "usd", "to_currency": "kes", ...}
-> HTTP 201 with from_currency=USD, to_currency=KES (silently corrected)
POST /quotes {"from_currency": "Usd", "to_currency": "kEs", ...}
-> HTTP 201 (silently corrected)
```

**Why it matters in production:** The bug it hides — a client typo
in a hard-coded enum — never surfaces as a client error and never
gets fixed. The customer eventually sees a strange transaction with
unexpected metadata or fails some downstream system that assumes
uppercase.

**Fix:** Reject non-uppercase codes with 400 before any business
logic runs.

**SPEC reference:** §2 currency code validation.

---

## Tentative observations

None promoted to bug status. Items that initially looked suspicious
but resolved on closer inspection are listed in the next section.

## What I deliberately did not flag

These are items I considered and rejected as either non-bugs or
issues whose framing as "bugs" would be padding.

- **Global `_execute_lock = threading.Lock()` serialising every
  execute through one mutex.** This is a performance concern (a
  per-quote lock would be more granular) but not a correctness
  defect. The actual correctness bug here is the TOCTOU race that
  this lock fails to prevent — already captured as Bug #3. Flagging
  the lock granularity separately would be padding.

- **`Optional[Dict[str, Decimal]]` typing in `rates.py` instead of
  `dict[str, Decimal] | None`.** Outdated for Python 3.10+ but
  semantically identical. Style, not bug.

- **`SPREAD_BPS = Decimal("0.005")` named "BPS" but holding a
  fractional value.** 50 basis points = 0.5% = 0.005 in fractional
  form, so the name is misleading but the math is right (the
  variable is consumed via `mid * (1 ± SPREAD_BPS)` which is
  consistent with the fractional value). Naming nit.

- **`logging.basicConfig(...)` at module load + `import json`
  inside function bodies.** Both are code style. Not breaking
  anything.

- **`refresh()` doesn't actually fetch from upstream; it re-applies
  the seed.** The README in `planted_bugs/` says "In production this
  would hit exchangeratesapi.io" — explicitly stubbed for the
  exercise. Flagging it would be flagging the assignment scaffolding.

---

## Summary

| Severity | Count | Bugs |
|---|---|---|
| Blocker | 4 | #1 cross-pair routing, #2 spread direction, #3 TOCTOU double-execute, #4 execute recomputes rate |
| Major | 4 | #5 idempotency cross-quote replay, #6 flat 400 status codes, #7 float arithmetic, #8 missing X-Correlation-ID echo |
| Minor | 1 | #9 silent uppercase normalization |

**The four blockers all relate to direct customer-money harm.** A
production deployment of `planted_bugs/` as-is would lose money on
every cross-pair trade (Bug #1), every direct trade (Bug #2),
double-charge under any concurrent retry (Bug #3), and ignore the
quote contract under any rate movement (Bug #4). Of these, Bug #1 is
the most severe (orders of magnitude wrong) and Bug #3 is the most
likely to fire under realistic load.

The four majors are SPEC-violating contract bugs that don't directly
move money but corrupt the API contract clients depend on
(idempotency, status codes, observability, precision).

The single minor is included because it's an explicit SPEC §2
violation, not because it's the lowest-impact thing I could find.
