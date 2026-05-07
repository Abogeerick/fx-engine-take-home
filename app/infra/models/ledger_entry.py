"""Ledger entries table per SPEC §4 -- append-only, signed amount."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Numeric, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.models import Base
from app.infra.models.types import UtcDateTime


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (
        CheckConstraint(
            "currency IN ('USD','EUR','KES','NGN')",
            name="ck_ledger_currency_supported",
        ),
        CheckConstraint("amount != 0", name="ck_ledger_amount_nonzero"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    execution_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("executions.id", ondelete="RESTRICT"), nullable=False
    )
    customer_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
