"""Balances table per SPEC §4.

Composite primary key (customer_id, currency); a customer has at most
one row per supported currency. The non-negative invariant is
enforced by a CHECK constraint at the database layer (Postgres and
SQLite both honor it). The domain ``Balance`` value object is the
in-memory companion guard.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.models import Base


class Balance(Base):
    __tablename__ = "balances"
    __table_args__ = (
        CheckConstraint("amount >= 0", name="ck_balances_non_negative"),
        CheckConstraint(
            "currency IN ('USD','EUR','KES','NGN')",
            name="ck_balances_currency_supported",
        ),
    )

    customer_id: Mapped[UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    currency: Mapped[str] = mapped_column(String(3), primary_key=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
