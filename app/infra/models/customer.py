"""Customers table per SPEC §4."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.models import Base
from app.infra.models.types import UtcDateTime


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(),
        nullable=False,
        server_default=func.current_timestamp(),
    )
