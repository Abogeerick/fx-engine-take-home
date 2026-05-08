"""HTTP middleware: ``X-Correlation-ID`` propagation + request logging.

The correlation ID is echoed from the request header or generated
fresh; either way it is bound to ``structlog.contextvars`` so every
log emitted during the request (including from deep service code)
auto-includes it without the call site knowing.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from uuid import uuid4

import structlog
from starlette.requests import Request
from starlette.responses import Response

from app.observability import get_logger

log = get_logger(__name__)

_HEADER = "X-Correlation-ID"


async def correlation_id_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    incoming = request.headers.get(_HEADER)
    correlation_id = incoming if incoming else str(uuid4())

    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(correlation_id=correlation_id)

    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception(
            "request.failed",
            method=request.method,
            path=request.url.path,
            latency_ms=round((time.perf_counter() - start) * 1000, 2),
        )
        raise

    latency_ms = round((time.perf_counter() - start) * 1000, 2)
    log.info(
        "request.completed",
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
        latency_ms=latency_ms,
    )

    response.headers[_HEADER] = correlation_id
    return response
