"""Domain services -- orchestrators that compose multiple repositories
inside a single transaction.

Services do not own session or transaction lifecycle. Callers wrap
each service invocation in ``async with session.begin():`` so the
two-leg execute path commits atomically (or rolls back as a unit).
"""

from app.services.execute import ExecuteOutcome, ExecuteRequest, execute_quote
from app.services.quote_pricing import (
    PriceQuote,
    QuoteSourceUnavailable,
    SameCurrencyError,
    quote_pricing,
)

__all__ = [
    "ExecuteOutcome",
    "ExecuteRequest",
    "PriceQuote",
    "QuoteSourceUnavailable",
    "SameCurrencyError",
    "execute_quote",
    "quote_pricing",
]
