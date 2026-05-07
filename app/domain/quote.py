"""Quote domain types: routing enum, TTL constant, expiry helper.

Pure logic -- no I/O. The persistence-side ``Quote`` ORM model lives
in ``app/infra/models/quote.py``; that one is mutable, this one
classifies and carries policy constants.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

QUOTE_TTL = timedelta(seconds=60)


class Routing(StrEnum):
    DIRECT = "direct"
    VIA_USD = "via_USD"
    VIA_EUR = "via_EUR"


def is_expired(*, expires_at: datetime, now: datetime) -> bool:
    """Per SPEC §6: a quote with ``expires_at <= now`` returns 410 on execute.

    Both inputs must be tz-aware; the function does not read the wall
    clock, so callers pass ``Clock.now()`` (or a ``FrozenClock`` value
    in tests).
    """
    if expires_at.tzinfo is None or now.tzinfo is None:
        raise ValueError("is_expired requires tz-aware datetimes")
    return expires_at <= now
