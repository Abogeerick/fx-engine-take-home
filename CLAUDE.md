# CLAUDE.md — Agent Operating Instructions

You are Claude Code, working on the Umba FX Engine take-home assignment.
This document tells you how to work. The technical specification is
`SPEC.md` and is the source of truth for *what* to build.

If anything in this file conflicts with `SPEC.md`, the spec wins. If
both are silent, stop and ask.

---

## 1. Operating principles

1. **The spec is the contract.** Do not invent features the spec does
   not require. Do not skip requirements the spec marks as required.
2. **Surface uncertainty early.** If you're unsure whether the spec
   covers a case, stop and ask before writing code. A 30-second
   question beats 30 minutes of throwaway code.
3. **Small commits, meaningful messages.** Each commit should compile
   and pass its own subset of tests. The git history is graded —
   treat it as a deliverable.
4. **Tests prove behavior; types prove shape.** Write the failing test
   before the implementation when behavior is non-obvious. Don't write
   tests after the fact to ratify whatever the code happens to do.
5. **You are not the senior engineer.** I am. Your job is to draft
   and execute; my job is to decide. Flag trade-offs explicitly when
   you encounter them — don't pick silently.

## 2. Stack and tooling (locked)

- **Language:** Python 3.11+
- **Web framework:** FastAPI
- **DB:** PostgreSQL 15+ in dev/prod, SQLite for fast unit/property tests
- **DB driver:** `asyncpg` via SQLAlchemy 2.x async, or `psycopg[binary]`
  if asyncpg adds friction. Pick one and stick with it; do not mix.
- **Migrations:** Alembic
- **Validation/serialization:** Pydantic v2
- **Decimal:** `decimal.Decimal` only. Float is **forbidden** anywhere
  amounts, rates, or balances appear. If you find yourself writing
  `float(...)` for a money value, stop.
- **Test runner:** pytest + pytest-asyncio + Hypothesis for property
  tests
- **Lint/format:** ruff (lint + format), mypy in strict mode for
  `app/domain/`, looser elsewhere
- **HTTP client:** httpx (async)
- **Observability:** structlog for JSON logging, prometheus-client for
  /metrics, plain UUID4 correlation IDs
- **Container:** docker-compose for Postgres in dev/test
- **Build/run:** Makefile with targets: `up`, `down`, `test`,
  `test-concurrency`, `load-test`, `lint`, `format`, `migrate`

Do not introduce additional libraries without flagging. Specifically do
not introduce: Celery, Redis, an ORM other than SQLAlchemy, or any
"AI agent framework." None of these are needed.

## 3. Repository layout (locked)

```
fx-engine/
├── app/
│   ├── api/              # FastAPI routes, request/response models
│   ├── domain/           # Pure business logic — no I/O
│   ├── infra/            # DB, rate provider, clock, idempotency store
│   └── observability/    # logging, metrics, correlation IDs
├── tests/
│   ├── unit/
│   ├── property/
│   ├── concurrency/
│   └── integration/
├── docs/
│   ├── SPEC.md
│   ├── DECISIONS.md
│   └── REVIEW.md
├── scripts/
│   └── load_test.py
├── planted_bugs/         # the assignment-provided code under review
├── CLAUDE.md
├── README.md
├── docker-compose.yml
├── pyproject.toml
└── Makefile
```

`app/domain/` is the heart of the engine. It must be importable and
unit-testable without a database, without HTTP, without a clock, and
without a rate provider. Those are injected at the `app/api/`
composition root.

## 4. Hard rules

These are non-negotiable. If a rule conflicts with something the user
asked for, **stop and ask** — do not silently break the rule.

1. **No floats for money.** Ever. Not in calculations, not in JSON, not
   in tests. Decimal everywhere; serialize to string at API boundary.
2. **No bare `except:`** anywhere. Always catch a specific exception.
3. **No `print()`** outside of `scripts/`. Use the structured logger.
4. **No SQL string concatenation.** Parameterized queries only.
5. **No mutable default arguments.** Standard Python footgun.
6. **No reading `datetime.utcnow()` directly in domain code.** Inject a
   `Clock` protocol so tests can advance time. Domain code calls
   `clock.now()`.
7. **No reading env vars in domain code.** Config is loaded once at the
   composition root and passed in.
8. **No async/sync mixing in a single function.** If a function is
   async, its dependencies are async. Don't `asyncio.run()` inside an
   already-running event loop.
9. **No `time.sleep` in tests.** Use the injectable clock or pytest
   fixtures. Sleep-based tests are flaky and signal a design problem.
10. **No new public endpoints not in SPEC.md §6.** If an endpoint feels
    necessary, ask first.

## 5. Workflow expectations

For each step in the execution plan I give you:

1. **Restate the goal in one sentence** before writing code. If your
   one-sentence summary doesn't match what I asked for, stop.
2. **List the files you will create or modify.** Don't touch files
   outside that list without flagging.
3. **Write the test first** when the behavior is specified in SPEC.md
   §12 (the graded tests).
4. **Implement.** Keep the diff minimal — no opportunistic refactoring
   of unrelated code.
5. **Run the tests.** Report what passed and what failed. Do not claim
   "tests pass" without running them.
6. **Show me the diff** for review before committing.
7. **Commit** with a message in this form:
   `<scope>: <imperative summary>` (e.g. `domain: add quote expiration check`).
   Body explains *why*, not *what*.

If you finish a step early and want to do "just one more thing" — stop.
Surface the suggestion as a question, not a fait accompli.

## 6. Things that are graded but easy to miss

The assignment rubric explicitly grades:

- **Decimal precision throughout.** Property tests required.
- **Concurrency safety on execute.** Test required.
- **Idempotency on execute.** Test required.
- **Atomic two-leg execution.** Demonstrate failure of leg 2 rolling
  back leg 1.
- **Rate-source failure handling.** Documented policy + test.
- **Observability.** Correlation IDs linking quote → execute. Show
  example log output in README.
- **Git history.** Small, meaningful commits. One giant initial
  commit is a red flag.
- **Process artifacts.** SPEC.md, CLAUDE.md, DECISIONS.md, REVIEW.md
  are graded the same as the code.

When you finish the engine, the **last** task before submission is to
re-read this list and confirm each item is demonstrably done. Do not
assume "the test exists" means "the test proves the property."

## 7. The planted_bugs review

When we get to `planted_bugs/`:

- **Do not modify the code there.** Read-only.
- **Do not assume bugs you haven't verified.** Run the code, write
  exploratory tests, confirm the bug reproduces before flagging.
- **Rank by production impact**, not by how clever the bug is.
- **False positives count against the grade.** If you're unsure
  whether something is a real bug, label it as a tentative observation
  rather than promoting it to a blocker.
- **Connect bugs to SPEC.md invariants.** If a bug violates an
  invariant we wrote in our own spec, say so explicitly — that's the
  senior framing the assignment is asking for.

The output is `docs/REVIEW.md`. Format per assignment: severity, what's
wrong, why it matters in production, how to fix.

## 8. Things to flag, not fix silently

If you encounter any of these, stop and surface to me:

- A library version with a known CVE.
- A test that passes only because the assertion is too weak (e.g.
  `assert result is not None` for a value that should equal something
  specific).
- A spec contradiction or ambiguity.
- A failing test that you're tempted to mark `xfail` or `skip`.
- A performance issue that requires architectural change to fix
  (e.g. an N+1 in a hot path).
- A case where you'd need to mock something the spec assumes is real.

For each, flag with a `# FLAG:` comment in the code *and* a chat
message, so we have both a code marker and a conversation point.

## 9. What "done" looks like for a step

A step is done when:

1. The acceptance criterion I gave you is met.
2. New tests pass and existing tests still pass (`make test`).
3. Lint and type checks pass (`make lint`).
4. The diff is minimal — no unrelated changes.
5. There's a commit with a clear message.
6. You have surfaced any flags, questions, or trade-offs you
   encountered.

If any of those is not true, the step is not done. Don't claim done.

## 10. Communication style

- Be terse. I read fast and I don't need preambles.
- When you ask a question, give me the 2-3 options you're choosing
  between with one line of trade-off each. Don't ask open-ended
  "what should I do?" — that's my job to ask, not yours.
- When you flag a problem, propose a fix in the same message. "Here's
  what I see, here's what I'd do, want me to proceed?"
- Don't apologize for things that aren't mistakes. Don't over-praise
  the spec or the plan. Save the social tokens for moments that matter.

---

**Final note.** This take-home is being graded on judgment as much as
on code. A 700-line implementation with crisp commits, a clean spec, a
sharp REVIEW.md, and a DECISIONS.md that honestly says "I delegated X
to you, kept Y for myself, caught you wrong about Z" beats a 2,000-line
implementation with a one-line commit history. Aim for the former.