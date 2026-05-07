"""Custom column types -- portability shims for dialect quirks.

``UtcDateTime`` exists because ``DateTime(timezone=True)`` on SQLite
does not round-trip ``tzinfo``: writes accept tz-aware datetimes,
reads return naive ones. The application contract (per ``Clock``)
is "always UTC-aware on the way in", so the read path attaches UTC.
On Postgres the underlying TIMESTAMPTZ already carries tz; the
decorator is a no-op there.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


class UtcDateTime(TypeDecorator[datetime]):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("UtcDateTime requires tz-aware datetimes on write")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            # SQLite reads back naive; we wrote UTC, so assume UTC.
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    # Required by mypy strict for TypeDecorator subclasses.
    def copy(self, **kw: Any) -> UtcDateTime:
        return UtcDateTime()
