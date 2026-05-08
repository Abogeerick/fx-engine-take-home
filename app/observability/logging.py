"""Structlog JSON logger configuration.

Per SPEC §9: every domain event emits one JSON log line with fields
``event``, ``correlation_id``, and the request-scope identifiers
(``quote_id``, ``execution_id``, ``customer_id``) where relevant.

Correlation IDs flow via ``structlog.contextvars`` -- the
``X-Correlation-ID`` middleware binds the id at request entry, and
every log call within the request scope auto-includes it without
the call site needing to know.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog


def configure_logging(*, level: str = "INFO", json_output: bool = True) -> None:
    """Initialise structlog with a JSON renderer."""
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: Any
    if json_output:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=False)

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)
