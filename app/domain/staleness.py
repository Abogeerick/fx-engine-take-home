"""Rate-cache staleness tiers per SPEC §8.

Classification is business logic (the thresholds drive HTTP status
codes and quoting decisions), so the enum lives in the domain layer.
The repository imports from here; the dependency must not run the
other way.

Thresholds:
  - fresh:           age <= 60s
  - cached:          60s < age <= 10 min
  - stale_unusable:  age > 10 min  (refuse to quote -- HTTP 503)
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

FRESH_THRESHOLD = timedelta(seconds=60)
CACHED_THRESHOLD = timedelta(minutes=10)


class StalenessTier(StrEnum):
    FRESH = "fresh"
    CACHED = "cached"
    STALE_UNUSABLE = "stale_unusable"


def classify(*, fetched_at: datetime, now: datetime) -> StalenessTier:
    """Map a (fetched_at, now) pair to a staleness tier per SPEC §8.

    Both inputs must be tz-aware; mixing naive and aware datetimes
    raises rather than producing silently-wrong arithmetic.
    """
    if fetched_at.tzinfo is None or now.tzinfo is None:
        raise ValueError("classify requires tz-aware datetimes")
    age = now - fetched_at
    if age <= FRESH_THRESHOLD:
        return StalenessTier.FRESH
    if age <= CACHED_THRESHOLD:
        return StalenessTier.CACHED
    return StalenessTier.STALE_UNUSABLE
