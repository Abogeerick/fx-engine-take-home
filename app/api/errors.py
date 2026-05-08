"""Exception -> HTTP status mapping.

A single mapping table keeps the wire-level contract honest. SPEC §10
is the source of truth; this module is the literal translation.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.services.quote_pricing import QuoteSourceUnavailable, SameCurrencyError


def _err_body(error: str, message: str, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"error": error, "message": message}
    body.update(extra)
    return body


async def same_currency_handler(_: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content=_err_body("invalid_request", str(exc)),
    )


async def quote_source_unavailable_handler(_: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content=_err_body("rate_source_unavailable", str(exc)),
    )


async def value_error_handler(_: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content=_err_body("invalid_request", str(exc)),
    )


def _strip_ctx(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop the ``ctx`` key, which Pydantic populates with the original
    BaseException instance (not JSON-serialisable).
    """
    return [{k: v for k, v in e.items() if k != "ctx"} for e in errors]


async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content=_err_body(
            "invalid_request",
            "request validation failed",
            details=_strip_ctx(exc.errors()),
        ),
    )


def register_handlers(app: Any) -> None:
    app.add_exception_handler(SameCurrencyError, same_currency_handler)
    app.add_exception_handler(QuoteSourceUnavailable, quote_source_unavailable_handler)
    app.add_exception_handler(ValueError, value_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
