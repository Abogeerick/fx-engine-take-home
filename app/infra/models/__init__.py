"""SQLAlchemy 2.x ORM models for the FX engine.

A single ``DeclarativeBase`` is exposed here so Alembic's
``target_metadata = Base.metadata`` picks up every table. The module
imports each model below the ``Base`` definition; this works because
Python sees ``Base`` in the partially-initialised package namespace
when each model module's ``from app.infra.models import Base`` runs.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


# Imported below ``Base`` definition on purpose -- registers tables
# on Base.metadata. Order does not matter for metadata registration.
from app.infra.models.balance import Balance  # noqa: E402
from app.infra.models.customer import Customer  # noqa: E402
from app.infra.models.execution import Execution  # noqa: E402
from app.infra.models.ledger_entry import LedgerEntry  # noqa: E402
from app.infra.models.quote import Quote  # noqa: E402
from app.infra.models.rate import Rate  # noqa: E402

__all__ = [
    "Balance",
    "Base",
    "Customer",
    "Execution",
    "LedgerEntry",
    "Quote",
    "Rate",
]
