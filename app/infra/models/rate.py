"""Rates table per SPEC §4.

One row per direct pair, upserted on each refresh. ``mid_rate`` is
``quote_currency`` per ``base_currency`` per the convention in SPEC
§2 (e.g. (USD, KES) with mid_rate=130.00 means 1 USD = 130 KES).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.models import Base
from app.infra.models.types import UtcDateTime


class Rate(Base):
    __tablename__ = "rates"
    __table_args__ = (CheckConstraint("mid_rate > 0", name="ck_rates_positive"),)

    base_currency: Mapped[str] = mapped_column(String(3), primary_key=True)
    quote_currency: Mapped[str] = mapped_column(String(3), primary_key=True)
    mid_rate: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
