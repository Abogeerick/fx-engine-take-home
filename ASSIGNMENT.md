# Senior Backend Engineer (AI-Native) — Take-Home Assignment

## Overview

Design and implement a foreign exchange (FX) engine that handles currency
conversions between USD, EUR, KES, and NGN, with per-customer balance
accounts. The system should be production-ready in its design approach,
though we know this is a time-boxed exercise.

We expect you to use AI coding tools (Claude Code, Codex, Cursor — pick
your poison). The role at Umba is explicitly AI-native, so we want to see
how you work with these tools, not how you'd write every line by hand.
We're more interested in your **process and judgment** than in raw code
volume.

**Time budget:** treat this as 1–2 focused days of work. Ship when you're
satisfied, not when you've run out of time. Tell us in the README what
you'd do with another day.

---

## Part 1 — Build the FX engine

### Core operations

1. **Generate FX quotes.** Convert an amount from one currency to another.
   Return exchange rate, final amount, quote ID, expiration time. Quotes
   valid for 60 seconds.
2. **Execute FX transactions.** Accept a quote ID, validate it hasn't
   expired, then debit the source-currency balance and credit the
   destination-currency balance for the customer. Atomic: both legs
   succeed or neither.
3. **Update exchange rates.** Pull from a public source (e.g.
   https://exchangeratesapi.io/ free tier). Rates include buy/sell
   spreads. Track last-updated timestamp.
4. **Customer balances.** Minimal: create a customer, view balances per
   currency, manually credit a balance (test fixture). `execute` reads
   and writes balances.

### Currency pairs

USD/KES, USD/NGN, USD/EUR, EUR/KES, EUR/NGN, EUR/USD, KES/NGN, NGN/KES,
plus all inverses. Cross pairs without a direct quote must route through
USD or EUR — document your routing rule and how spreads compound.

### Required (not bonus)

The original version of this assignment had several of these as "bonus."
Given AI tools, we're promoting them to required, and we want **evidence
that they actually work** — tests, load-test output, or a script we can
run.

- **Decimal precision throughout.** Declare your rounding mode and
  per-currency minor units in `SPEC.md`. Property-based tests over
  random amounts and pairs (Hypothesis or similar).
- **Concurrency safety on execute.** A test that fires N parallel
  executions of the same quote ID and asserts exactly one succeeds.
- **Idempotency on execute.** Client retries with the same idempotency
  key must not double-execute. Test it.
- **Atomic two-leg execution.** Debit and credit happen together or not
  at all. Demonstrate what happens when the second leg would push a
  balance negative, or when the process is interrupted mid-execute.
- **Rate-source failure handling.** What happens when the rates API is
  down, slow, or returns stale data? Document the policy in `SPEC.md`
  and demonstrate it.
- **Observability.** `/healthz`, `/metrics` (or structured logs),
  correlation/trace IDs linking quote → execute events. Show example log
  output in the README.

### Constraints / simplifications

- Skip auth/authz.
- SQLite or Postgres is fine; pure in-memory is **not** — we need to
  test concurrency.
- Use any backend stack you're comfortable with. Our team uses Python
  (Flask/FastAPI). Pick what lets you ship fastest.

---

## Part 2 — Process artifacts

These are first-class deliverables, graded the same as the code.

- **`SPEC.md`** — the technical spec you wrote *before* prompting.
  Inputs/outputs, invariants, error semantics, concurrency model,
  rounding rules, what's out of scope. Aim for one page; precision over
  volume.
- **`AGENTS.md` or `CLAUDE.md`** — the constraints and instructions you
  gave the agent.
- **`DECISIONS.md`** — one page covering:
  - The main trade-offs you made (architecture, scope, libraries).
  - Which decisions you made yourself vs. delegated to the AI.
  - What you accepted, rejected, or overrode from the AI's suggestions, and why.
  - One thing the AI got wrong and how you caught it.
  - What you did *not* trust without verifying.
- **Git history.** Small, meaningful commits. We will look at this. One
  "initial commit" containing everything is a red flag.

You don't need to share verbatim transcripts. We want your reasoning,
not raw logs.

---

## Part 3 — Code review exercise

We've shipped you an AI-generated FX engine in `planted_bugs/`. It runs
and its tests pass. We've planted multiple distinct issues in the
implementation. Review it as if a teammate opened a PR with this code
and asked for your sign-off — identify the problems, explain the
production impact, and propose fixes.

In `REVIEW.md`, for each issue:

- **Severity** (blocker / major / minor / nit)
- **What's wrong** and **why it matters in production**
- **How you'd fix it** (a sentence is fine; code if it's clarifying)

### What we're scoring

- **Severity ranking and reasoning.** Ordering bugs correctly by
  production impact matters more than finding every one of them.
- **Production framing.** "This is wrong" is a junior review. "This
  breaks under N concurrent retries because…" is a senior review.
- **Judgment about what *not* to flag.** A tight list of real bugs beats
  a long list that includes style nits and false alarms. False
  positives count against you. If you're unsure whether something is a
  real issue, label it as such — don't silently promote tentative
  observations to "bug."
- **Connecting bugs to the spec.** If something violates an invariant
  you wrote in `SPEC.md`, say so explicitly.

We are not grading on hit-count. Finding every planted issue is not
expected; finding most of them while keeping the noise floor low is.

You may run the code, write your own tests against it, and prompt your
AI tools to help review — same as you would on a real PR. Tell us what
you used.

---

## What we're evaluating

| Area | What good looks like |
|---|---|
| **Spec quality** | Tight, decisive, unambiguous. Explicit on rounding, concurrency, failure modes. |
| **Process** | You know what you delegated and what you owned. You caught the AI being wrong. |
| **Correctness** | Decimal handling is right. Concurrency tests pass. Idempotency works under retry. Two-leg execute is atomic. |
| **Code review** | You spot real bugs, rank them by impact, and explain *why* — not just *what*. |
| **System thinking** | Observability, failure handling, and atomic-transaction reasoning are real, not decorative. |
| **Code quality** | Readable, organized, no dead code. AI-generated does not mean unedited. |

---

## Deliverables

1. Repository link with:
   - Source code for the FX engine
   - `SPEC.md`, `DECISIONS.md`, `AGENTS.md` or `CLAUDE.md`
   - `REVIEW.md` (the planted-bug review)
   - `README.md` with setup, how to run tests, known limitations, what
     you'd do with more time
2. Estimated wall-clock and active-engagement time
3. Anything you want us to know about the process or the assignment

If anything's unclear, make a reasonable assumption and document it in
`SPEC.md` — we want to see how you handle ambiguity. Email
tiernan@umba.com if blocked.

Good luck.
