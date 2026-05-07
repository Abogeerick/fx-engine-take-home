# DECISIONS — Umba FX Engine

A running log of decisions made during implementation: trade-offs,
what was delegated to the AI vs. owned by the engineer, and what was
overridden after agent suggestions. The deliverable form (one tight
page) will be assembled from this log near submission. For now, this
is the working scratch.

## Step 1 — domain primitives

### Balance is a subclass of Money, not a flag on Money
**Decision owner:** AI proposed, engineer approved.
**Why:** A `non_negative=True` flag is primitive obsession. Subclass
makes the invariant load-bearing in the type system; functions
declaring `b: Balance` rely on `b.amount >= 0` without runtime
re-validation. Plain `Money` stays available for ledger entries
(signed) and arithmetic intermediates that may legitimately be
negative.
**Cost:** One extra class.

### Decimal context set at `app/domain/__init__.py`, not `money.py`
**Decision owner:** AI proposed, engineer approved.
**Why:** Any domain module may construct Decimals (rates, spreads in
later steps), not just money. Setting context on package import means
importing *anything* from `app.domain` establishes prec=28 +
ROUND_HALF_EVEN per SPEC §3 ("set once at process start"). A unit
test asserts the context state after import to keep the invariant
honest.

### Money accepts int (coerced); rejects float and bool
**Decision owner:** Engineer override of AI's stricter design.
**AI's original proposal:** Reject everything that isn't `Decimal` —
including `int` — to force callers to be explicit.
**Engineer's override:** Accept `int` (coerce via `Decimal(int)`,
which is exact), reject `float` and `bool`.
**Why:** The harm asymmetry is the point of the no-floats rule.
`float(0.1)` corrupts; `int(10)` does not. Hypothesis-generated
integers in upcoming property tests would otherwise force
`Decimal(str(generated_int))` boilerplate at every call site for
zero safety value. The Pydantic boundary on the API layer will
still validate inbound JSON.
**Bool note:** `bool` is a subclass of `int` in Python, so the
bool-reject branch must run before the int-coerce branch. There is
an explicit unit test for `Money(amount=True, …)` raising.

### `Balance(1) - Balance(3)` raises at construction
**Decision owner:** AI proposed, engineer flagged for revisit.
**Why kept for now:** `type(self)(...)` in arithmetic propagates the
subclass, and `Balance.__post_init__` re-validates non-negativity, so
subtracting balances below zero fails loudly at the type boundary.
**Why this might bite later:** The execute transaction will read a
balance, validate `from_balance >= from_amount`, then compute the
new balance. If subtraction can raise, callers can't write
`new = balance - amount` even after the check; they have to detour
through `Money` types or use `try/except`. Cleaner alternative:
`Balance - Balance` returns `Money` (signed), and the caller wraps
back into `Balance` if non-negativity is part of the contract.
**Plan:** Leave as-is for step 1. Reassess at step 3 (execute path).
If it forces awkward calling code, switch arithmetic to return `Money`.

### `Money.__post_init__` calls `Money.__post_init__(self)` from `Balance`, not `super()`
**Decision owner:** Engineer (resolving a runtime failure).
**Why:** `@dataclass(slots=True)` replaces the class object after
class definition, leaving `super()`'s `__class__` cell pointing at a
class that's no longer in the MRO. Calling the parent method by name
is the documented workaround. Documented inline at the call site so
a future maintainer doesn't "fix" it back to `super()`.

### Strict `filterwarnings = ["error"]` in pytest config
**Decision owner:** AI proposed, engineer approved.
**Why:** Catches deprecation and resource warnings at the test
boundary instead of letting them rot. No ignore list needed on
Python 3.13.

### Python 3.13, not 3.12 (CLAUDE.md says "3.11+")
**Decision owner:** Engineer (preference 3.12), AI (substitution 3.13).
**Why 3.13 instead of 3.12:** 3.12 was not installed locally; only
3.13 (Microsoft Store) and 3.14 (current Python.org default) were
present. The engineer pre-approved 3.13 in the same instruction
that rejected 3.14 ("3.13 is also fine, 3.14 is not yet a sane
production target"). 3.13 is current stable as of mid-2026 and
satisfies the spirit of the pushback (off the bleeding edge).
**Why not 3.14:** First run set up the venv on 3.14; pytest-asyncio
shipped a deprecated `asyncio.get_event_loop_policy()` call that
3.14 raises a `DeprecationWarning` for, which the strict
`filterwarnings` config converted into a test-collection error.
The fix on 3.14 was a narrow filter ignore; on 3.13 the call is
not deprecated and the ignore is not needed. CLAUDE.md §2's
"Python 3.11+" is a floor, not a directive to use the bleeding edge.
**Side effect uncovered by the version downgrade:** `super()` in
`Balance.__post_init__` raised on 3.13 — the dataclass-slots
inheritance footgun. 3.14 had silently let it through, meaning the
3.14 test pass was a false positive. Caught by the version pushback,
not by tests. Documented separately above.

### `planted_bugs/` excluded from ruff
**Decision owner:** AI proposed, engineer approved (implicit via
CLAUDE.md §7).
**Why:** Read-only per the assignment. Linting it would either
require modifications (which we cannot make) or generate noise on
every `make lint` run. `extend-exclude = ["planted_bugs", ".venv"]`
in `[tool.ruff]`.

## Step 2 — persistence layer

### Repository signature: `credit/debit(session, customer_id, money)`, no separate `currency` arg
**Decision owner:** AI override of engineer's listed signature.
**Engineer's listing (informal):** `credit(customer_id, currency, money)`.
**Implementation:** `credit(session, customer_id, money: Money)`.
**Why:** ``Money`` already carries its currency. A separate
``currency`` parameter invites mismatch-vs-payload checks for no
real benefit -- the type system already enforces consistency.
Documented for engineer review; revert if explicit double-spec is
preferred.

### `StalenessTier` lives in `app/domain/staleness.py`, not `app/infra/`
**Decision owner:** Engineer (in step-2 instructions); AI executed.
**Why:** Tier classification is business logic -- the thresholds
(60s fresh, 10 min stale-unusable) drive HTTP status codes and
quoting decisions per SPEC §8. The repository imports `classify`
from the domain, not the other way around. Confirms the dependency
direction the layout enforces.

### `UtcDateTime` TypeDecorator added (extra file `app/infra/models/types.py`)
**Decision owner:** AI flagged, engineer to review.
**Why:** `DateTime(timezone=True)` does not round-trip `tzinfo`
through SQLite -- writes accept tz-aware datetimes but reads return
naive ones. The application contract per ``Clock`` is "always
UTC-aware on the way in", so the `process_result_value` hook
re-attaches UTC. On Postgres the `TIMESTAMPTZ` column already
carries tz; the decorator is a no-op there.
**Detection story:** caught by the strict `classify()` guard in
`app/domain/staleness.py` -- it raises on naive datetimes. Without
that guard the bug would have manifested later as silently-wrong
freshness classifications on SQLite.

### Migration test caught by AC #1: `env.py` was overriding test-supplied URL
**Decision owner:** AI bug, engineer's AC #1 caught it.
**What broke:** `alembic/env.py` initially read `_settings = get_settings()`
at module load and unconditionally wrote that URL to the alembic
Config (`config.set_main_option("sqlalchemy.url", _settings.database_url)`).
That overrode any URL the test fixture set on the Config, so the
migration ran against the default `:memory:` SQLite DB while the
test queried a tmp-file DB -- alembic logged "Running upgrade ->
0001_initial_schema" but the tmp file stayed empty.
**Fix:** Inverted priority -- prefer the URL on the alembic Config
over `get_settings().database_url`. Settings is now the fallback for
plain `alembic upgrade head` from the CLI. Captured as a
`_resolve_url()` helper used by both online and offline migration
paths.
**Lesson:** AC #1 was load-bearing. Without "migration must run
cleanly on both backends" as an explicit test, this bug would have
surfaced in step 3 when the test for sticky idempotent failures
tried to insert into a non-existent `executions` row.

### CHECK-constraint test fix: SA Core `insert(...)` instead of `text(...)` with str(uuid)
**Decision owner:** AI bug, the test caught itself.
**What broke:** I tried to test the CHECK constraint with raw
`text("UPDATE balances SET amount = -1 WHERE customer_id = :cid")`
binding `str(uuid)`. SQLAlchemy's `Uuid()` type stores as 16-byte
BLOB on SQLite; the string parameter never matched, the UPDATE
affected zero rows, and the test reported a green CHECK constraint
that had never actually been exercised.
**Fix:** Switch to SA Core `insert(BalanceTable).values(...)` so the
type adapter binds the UUID correctly across both dialects.
**Lesson:** "Test passed" is not the same as "DB rejected the bad
input". When a test relies on the DB to enforce something, verify
the test actually reaches the DB layer with the expected payload.

### SQLite resource-leak from `with sqlite3.connect(...)`
**Decision owner:** Engineer's strict-warnings config caught it.
**What broke:** `with sqlite3.connect(db) as conn:` manages the
*transaction*, not the connection lifetime -- the file handle stays
open until garbage collection. Strict `filterwarnings = ["error"]`
escalated the resulting `ResourceWarning` to a test-collection error
on a downstream test (the unraisable warning fired during the
*next* test's setup, so the failure looked unrelated).
**Fix:** Wrap in `contextlib.closing(...)` so connections close
deterministically.
**Lesson:** This is exactly why CLAUDE.md keeps `filterwarnings`
strict -- a real leak that would have shown up under load surfaced
on the first test run instead.

### SQLite `FOR UPDATE` is a no-op; correctness preserved by file-level write serialization
**Decision owner:** AI proposed (per engineer's watch-list item);
engineer approved.
**Why:** SQLite has no row-level locking. SQLAlchemy generates the
`FOR UPDATE` clause on Postgres dialects and silently omits it on
SQLite. Concurrent writers serialize at the database file level,
which is correct (no torn writes) but worse for throughput. SQLite
tier tests don't exercise blocking semantics; the Postgres
integration test does, with two concurrent sessions and an
asyncio-timestamped assertion that B doesn't complete until A's
hold elapses.

### Per-test cleanup pattern: `DELETE FROM` on SQLite, `TRUNCATE` on Postgres
**Decision owner:** AI proposed.
**Why:** SQLite has no `TRUNCATE`. The cleanup ordering matters
because `balances.customer_id` is FK to `customers`. Postgres
supports `TRUNCATE balances, customers, rates RESTART IDENTITY
CASCADE` in a single statement, which is faster and avoids the
ordering question.

### Test database: `fx_engine_test`, auto-created from the `postgres` admin DB
**Decision owner:** AI proposed.
**Why:** Keeps integration test data isolated from a developer's
`fx_engine` dev DB. The session-scoped fixture creates the test DB
(if absent) by connecting to the always-present `postgres` system DB
in AUTOCOMMIT mode and running `CREATE DATABASE`. Cheap and idempotent.

### Module-level skip when Postgres unreachable
**Decision owner:** AI proposed.
**Why:** A developer running `make test-unit` shouldn't see noise
from a separate compose stack they didn't intend to start. The
integration `conftest.py` does a 2-second `SELECT 1` probe at
collection time and applies `pytest.mark.skipif` to the whole
module if it fails. `make test-integration` brings up compose first,
so the probe always succeeds in that flow.

## Step 3 — quotes, executions, ledger + execute orchestrator

### Balance arithmetic question (from step 1) — answer
**Decision owner:** Engineer asked at step 3 to revisit; AI answered.
**Outcome:** Keep `Balance(1) - Balance(3)` raising at construction.
The execute orchestrator works with ORM `Balance` rows whose
`amount` is `Decimal`, **not** with the domain `Balance` value
object. The ORM-level arithmetic (`row.amount = row.amount + money.amount`)
is where balance changes happen; the domain `Balance` type guards
construction-time invariants on Python objects flowing through pure
domain code, where "subtract a balance to go negative" is genuinely
an error. The two layers don't interfere.
**Lesson:** The "this might cause friction in execute" flag from
step 1 turned out to be a false alarm. Documented with the resolved
status so it doesn't get re-litigated later.

### Circular FK between quotes and executions: ORM-only on one side
**Decision owner:** AI proposed; engineer to review.
**Why:** SPEC §4 has `quotes.consumed_by_execution_id` referencing
`executions.id` *and* `executions.quote_id` referencing `quotes.id`.
Postgres can model this with `DEFERRABLE INITIALLY DEFERRED`; SQLite
cannot. Adding the second FK at the DB level would require batch-
mode `ALTER` on SQLite, which complicates the migration without
real safety gain.
**Resolution:** Keep `executions.quote_id -> quotes.id` as a DB FK
(the strong direction; an execution must always reference an
existing quote). For `quotes.consumed_by_execution_id ->
executions.id`, declare the relationship at the ORM layer only.
The integrity argument: the only writer is the execute orchestrator,
which sets `consumed_by_execution_id` in the same transaction that
inserted the executions row. A future bug that bypasses the
orchestrator would be the only way to write a stale value, and a
DB constraint there wouldn't have caught the *existence* problem
anyway -- it would just have made the migration uglier.

### Partial unique index on `executions.quote_id WHERE status='succeeded'`
**Decision owner:** SPEC §4 (engineer); AI verified portability.
**Verification:** Both Postgres and SQLite (>= 3.8) support partial
unique indexes natively. The `Index(...)` declaration uses
`postgresql_where=` and `sqlite_where=` so SQLAlchemy emits the
right DDL on each backend. Fresh upgrade-then-introspect on SQLite
showed the index `ix_executions_quote_succeeded` was created with
the WHERE clause intact -- no domain-level fallback needed.
**Why it's defence-in-depth, not the primary serialisation point:**
The race we're guarding against -- two parallel executes trying to
mark the same quote consumed -- is serialised by `SELECT ... FOR
UPDATE` on the quote row. The partial index is the backstop in
case a future bug reaches commit time without holding that lock;
it would convert a silent corruption into a loud `IntegrityError`.

### Pending-execution placeholder pattern: insert with `status='failed'`, update on success
**Decision owner:** AI proposed.
**Why:** The orchestrator must insert the executions row early so
the unique constraint on `(customer_id, idempotency_key)` catches
replay attempts. But "succeeded" and "failed" are the only valid
statuses (no "in_flight"), so we can't insert a neutral marker.
Inserting with `status='failed'` keeps the row outside the partial
unique index's match set; the orchestrator updates to `succeeded`
once business logic clears. Concurrent executes on the same quote
both insert `failed` placeholders, neither hits the partial index,
and the FOR UPDATE lock on the quote serialises the actual mutation.

### `response_body` assembled inside the transaction from post-flush ORM values
**Decision owner:** Engineer's hard requirement (AC #3); AI implemented.
**Why:** The replay path returns the persisted `response_body`
verbatim. If the body were assembled *after* commit -- e.g. by a
fresh `SELECT balances` -- replay would return a stale snapshot.
**How:** `BalanceRepository.debit/credit` flushes the UPDATE before
returning the ORM row, so `row.amount` already reflects the post-
update value. The orchestrator reads `debited_row.amount` and
`credited_row.amount` directly into the response_body dict, then
calls `ExecutionRepository.finalize_succeeded` which writes the
dict to the row. The whole sequence runs inside the caller's
`async with session.begin():`, so commit happens after the body
is persisted.
**Note on UPDATE RETURNING:** Postgres supports it explicitly; we
don't use `.returning()` because SQLAlchemy ORM's flush already
gives us the post-update value via the ORM identity map. The
contract -- "response body reflects post-update state, persisted
in the same transaction" -- is met either way.

### Atomicity test uses `monkeypatch` to inject a credit-leg fault
**Decision owner:** AI proposed.
**Why:** SPEC §12 requires demonstrating that a credit-leg failure
rolls back the debit. A real fault (e.g., DB connection drop) is
hard to provoke deterministically. Patching `BalanceRepository.credit`
to raise on a specific currency gives a reproducible, fast test.
The patch is reverted via `monkeypatch.undo()` before the retry
half of the test, which verifies the idempotency key is *not*
sticky after a DB-level rollback.

### Idempotency-key-reused-with-different-quote: 409, no DB write
**Decision owner:** SPEC §6/§10; AI implemented.
**Why:** This is the one path where SPEC says "no persistent state
change" on failure. The orchestrator enters a SAVEPOINT, fails on
the unique constraint, and the SAVEPOINT auto-rolls back. We then
read the existing row (from the outer transaction's view) and
compare quote_ids. Different quote_id -> 409 returned to caller; no
new execution row written. Same quote_id -> stored response_body
returned with HTTP 200 (the byte-identical replay path).

