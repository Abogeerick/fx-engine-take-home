"""Observability: structlog config, prometheus metrics, correlation IDs."""

from app.observability.logging import configure_logging, get_logger
from app.observability.metrics import (
    EXECUTE_TOTAL,
    IDEMPOTENT_REPLAY_TOTAL,
    QUOTE_TOTAL,
    RATE_FETCH_FAILURE_TOTAL,
    RATE_FETCH_LATENCY,
)

__all__ = [
    "EXECUTE_TOTAL",
    "IDEMPOTENT_REPLAY_TOTAL",
    "QUOTE_TOTAL",
    "RATE_FETCH_FAILURE_TOTAL",
    "RATE_FETCH_LATENCY",
    "configure_logging",
    "get_logger",
]
