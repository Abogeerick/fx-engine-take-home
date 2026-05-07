"""Executions table per SPEC §4."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.models import Base
from app.infra.models.types import UtcDateTime


class Execution(Base):
    __tablename__ = "executions"
    __table_args__ = (
        UniqueConstraint(
            "customer_id",
            "idempotency_key",
            name="uq_executions_customer_idempkey",
        ),
        # Partial unique: at most one succeeded execution per quote.
        Index(
            "ix_executions_quote_succeeded",
            "quote_id",
            unique=True,
            postgresql_where=text("status = 'succeeded'"),
            sqlite_where=text("status = 'succeeded'"),
        ),
        CheckConstraint(
            "status IN ('succeeded','failed')",
            name="ck_executions_status_valid",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    quote_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("quotes.id", ondelete="RESTRICT"), nullable=False
    )
    customer_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSON(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
