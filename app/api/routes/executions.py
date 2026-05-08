"""POST /executions -- atomic two-leg execute.

The orchestrator owns the SPEC §7 step sequence; this route is a
thin wrapper that wraps the call in ``async with session.begin():``
(the orchestrator's documented contract) and returns the
orchestrator's response_body verbatim with the supplied http_status.
"""

from __future__ import annotations

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from app.api.schemas import ExecuteRequestBody
from app.observability import EXECUTE_TOTAL, IDEMPOTENT_REPLAY_TOTAL, get_logger
from app.services import ExecuteRequest, execute_quote

router = APIRouter()
log = get_logger(__name__)


@router.post("/executions")
async def post_executions(
    body: ExecuteRequestBody,
    request: Request,
    idempotency_key_header: str | None = Header(None, alias="Idempotency-Key"),
) -> JSONResponse:
    # SPEC §6: body field takes precedence over header if both present.
    idempotency_key = body.idempotency_key or idempotency_key_header
    if not idempotency_key:
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_request",
                "message": "idempotency_key required (in body or Idempotency-Key header)",
            },
        )

    state = request.app.state
    factory = state.session_factory
    clock = state.clock

    async with factory() as session:
        async with session.begin():
            outcome = await execute_quote(
                session,
                ExecuteRequest(
                    quote_id=body.quote_id,
                    customer_id=body.customer_id,
                    idempotency_key=idempotency_key,
                ),
                clock,
            )

    if outcome.is_replay:
        IDEMPOTENT_REPLAY_TOTAL.inc()
    EXECUTE_TOTAL.labels(status=outcome.response_body.get("status", "unknown")).inc()

    log.info(
        "execute.completed",
        quote_id=str(body.quote_id),
        execution_id=outcome.response_body.get("execution_id"),
        customer_id=str(body.customer_id),
        http_status=outcome.http_status,
        outcome=outcome.response_body.get("status"),
        is_replay=outcome.is_replay,
    )

    return JSONResponse(
        status_code=outcome.http_status,
        content=outcome.response_body,
    )
