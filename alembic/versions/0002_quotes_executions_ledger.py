"""Add quotes, executions, ledger_entries.

Per SPEC §4. Builds on 0001 (customers, balances, rates).

Dialect notes:

* Partial unique index on ``executions.quote_id WHERE status =
  'succeeded'``: Postgres native; SQLite has supported partial
  indexes since 3.8 (2013). SQLAlchemy renders a dialect-aware
  ``WHERE`` clause via ``postgresql_where`` / ``sqlite_where``.
  This index is the defence-in-depth backstop for "at most one
  successful execution per quote" -- the primary serialisation
  point is ``SELECT ... FOR UPDATE`` on the quote row, but the
  index ensures the invariant survives even if a future bug
  reaches commit time without holding the lock.

* ``response_body`` uses the portable ``sa.JSON`` type. Postgres
  maps it to ``JSONB``, SQLite stores TEXT-with-JSON-in-it. Round-
  trip is exercised in integration tests.

* The circular FK pair is asymmetric: ``executions.quote_id`` ->
  ``quotes.id`` is a DB-level FK; ``quotes.consumed_by_execution_id``
  -> ``executions.id`` is enforced only at the ORM layer. SQLite
  cannot ``ALTER TABLE ADD CONSTRAINT FOREIGN KEY`` and the
  application is the only writer of ``consumed_by_execution_id``,
  always inside the same transaction as the executions insert.
  See DECISIONS.md.

* ``ledger_entries.amount`` is signed (debit < 0, credit > 0) per
  SPEC §4. The ``CHECK amount != 0`` rules out zero-magnitude
  entries that would corrupt downstream sums.

Revision ID: 0002_quotes_executions_ledger
Revises: 0001_initial_schema
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_quotes_executions_ledger"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SUPPORTED_CURRENCIES = "('USD','EUR','KES','NGN')"
_ROUTING_VALUES = "('direct','via_USD','via_EUR')"
_STATUS_VALUES = "('succeeded','failed')"


def upgrade() -> None:
    op.create_table(
        "quotes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "customer_id",
            sa.Uuid(),
            sa.ForeignKey("customers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("from_currency", sa.String(3), nullable=False),
        sa.Column("to_currency", sa.String(3), nullable=False),
        sa.Column("from_amount", sa.Numeric(20, 8), nullable=False),
        sa.Column("to_amount", sa.Numeric(20, 8), nullable=False),
        sa.Column("rate_applied", sa.Numeric(20, 8), nullable=False),
        sa.Column("routing", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        # No DB-level FK on consumed_by_execution_id -- see module docstring.
        sa.Column("consumed_by_execution_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            f"from_currency IN {_SUPPORTED_CURRENCIES}",
            name="ck_quotes_from_currency_supported",
        ),
        sa.CheckConstraint(
            f"to_currency IN {_SUPPORTED_CURRENCIES}",
            name="ck_quotes_to_currency_supported",
        ),
        sa.CheckConstraint(
            f"routing IN {_ROUTING_VALUES}",
            name="ck_quotes_routing_valid",
        ),
        sa.CheckConstraint("from_amount > 0", name="ck_quotes_from_positive"),
        sa.CheckConstraint("to_amount > 0", name="ck_quotes_to_positive"),
        sa.CheckConstraint("rate_applied > 0", name="ck_quotes_rate_positive"),
        sa.CheckConstraint(
            "from_currency != to_currency",
            name="ck_quotes_distinct_currencies",
        ),
    )

    op.create_table(
        "executions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "quote_id",
            sa.Uuid(),
            sa.ForeignKey("quotes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            sa.Uuid(),
            sa.ForeignKey("customers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("failure_reason", sa.String(64), nullable=True),
        sa.Column("response_body", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "customer_id",
            "idempotency_key",
            name="uq_executions_customer_idempkey",
        ),
        sa.CheckConstraint(
            f"status IN {_STATUS_VALUES}",
            name="ck_executions_status_valid",
        ),
    )

    op.create_index(
        "ix_executions_quote_succeeded",
        "executions",
        ["quote_id"],
        unique=True,
        postgresql_where=sa.text("status = 'succeeded'"),
        sqlite_where=sa.text("status = 'succeeded'"),
    )

    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "execution_id",
            sa.Uuid(),
            sa.ForeignKey("executions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            sa.Uuid(),
            sa.ForeignKey("customers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("amount", sa.Numeric(20, 8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            f"currency IN {_SUPPORTED_CURRENCIES}",
            name="ck_ledger_currency_supported",
        ),
        sa.CheckConstraint("amount != 0", name="ck_ledger_amount_nonzero"),
    )


def downgrade() -> None:
    op.drop_table("ledger_entries")
    op.drop_index("ix_executions_quote_succeeded", table_name="executions")
    op.drop_table("executions")
    op.drop_table("quotes")
