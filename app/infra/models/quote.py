"""Quotes table per SPEC §4."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Numeric, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.models import Base
from app.infra.models.types import UtcDateTime


class Quote(Base):
    __tablename__ = "quotes"
    __table_args__ = (
        CheckConstraint(
            "from_currency IN ('USD','EUR','KES','NGN')",
            name="ck_quotes_from_currency_supported",
        ),
        CheckConstraint(
            "to_currency IN ('USD','EUR','KES','NGN')",
            name="ck_quotes_to_currency_supported",
        ),
        CheckConstraint(
            "routing IN ('direct','via_USD','via_EUR')",
            name="ck_quotes_routing_valid",
        ),
        CheckConstraint("from_amount > 0", name="ck_quotes_from_positive"),
        CheckConstraint("to_amount > 0", name="ck_quotes_to_positive"),
        CheckConstraint("rate_applied > 0", name="ck_quotes_rate_positive"),
        CheckConstraint(
            "from_currency != to_currency",
            name="ck_quotes_distinct_currencies",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    customer_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    from_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    to_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    from_amount: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    to_amount: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    rate_applied: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    routing: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    # No DB-level FK -- see migration 0002 docstring and DECISIONS.md.
    consumed_by_execution_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
