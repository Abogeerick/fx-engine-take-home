"""Initial schema: customers, balances, rates.

Per SPEC §4. Quote / execution / ledger_entries tables land in step 3.

Dialect notes:

* ``Numeric(20, 8)`` is portable. SQLite stores values under NUMERIC
  affinity (TEXT-based) and SQLAlchemy's adapter round-trips
  ``Decimal`` exactly via ``str``. Postgres stores native NUMERIC.

* ``Uuid()`` (the cross-dialect type, not ``postgresql.UUID``) maps to
  native ``uuid`` on Postgres and a 16-byte BLOB on SQLite. Tests
  exercise insert+read on both backends to catch any type drift.

* CHECK constraints (``amount >= 0``, supported currency, positive
  rate) are enforced by both Postgres and SQLite >= 3.0. Tests assert
  the constraints reject invalid writes on each backend.

* ``DateTime(timezone=True)`` becomes ``TIMESTAMPTZ`` on Postgres and
  ISO-format TEXT on SQLite. The application contract is "always
  tz-aware on the way in"; ``FrozenClock`` and ``SystemClock`` both
  produce UTC-aware datetimes.

Revision ID: 0001_initial_schema
Revises:
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
    )

    op.create_table(
        "balances",
        sa.Column(
            "customer_id",
            sa.Uuid(),
            sa.ForeignKey("customers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("amount", sa.Numeric(20, 8), nullable=False),
        sa.PrimaryKeyConstraint("customer_id", "currency"),
        sa.CheckConstraint("amount >= 0", name="ck_balances_non_negative"),
        sa.CheckConstraint(
            "currency IN ('USD','EUR','KES','NGN')",
            name="ck_balances_currency_supported",
        ),
    )

    op.create_table(
        "rates",
        sa.Column("base_currency", sa.String(3), nullable=False),
        sa.Column("quote_currency", sa.String(3), nullable=False),
        sa.Column("mid_rate", sa.Numeric(20, 8), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("base_currency", "quote_currency"),
        sa.CheckConstraint("mid_rate > 0", name="ck_rates_positive"),
    )


def downgrade() -> None:
    op.drop_table("rates")
    op.drop_table("balances")
    op.drop_table("customers")
