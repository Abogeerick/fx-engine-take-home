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
