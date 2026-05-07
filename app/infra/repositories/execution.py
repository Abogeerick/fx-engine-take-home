"""Execution repository -- insert + idempotency-key lookup + outcome update.

The orchestrator inserts a placeholder row (status='failed', empty
``response_body``) inside a savepoint so the unique-constraint
collision on ``(customer_id, idempotency_key)`` becomes the
idempotency-replay signal. The orchestrator then updates status and
response_body to the final values before the outer transaction
commits.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.execution import ExecutionStatus, FailureReason
from app.infra.models import Execution


class ExecutionRepository:
    @staticmethod
    async def insert_pending(
        session: AsyncSession,
        *,
        execution_id: UUID,
        quote_id: UUID,
        customer_id: UUID,
        idempotency_key: str,
        now: datetime,
    ) -> Execution:
        """Insert a placeholder row.

        Status starts as 'failed' with an empty response_body; the
        orchestrator overwrites these once it knows the outcome. The
        partial unique index on ``quote_id WHERE status='succeeded'``
        does not match this row, so concurrent placeholders for the
        same quote do not collide here.

        The caller wraps this in ``begin_nested()`` so the
        ``IntegrityError`` from the unique constraint on
        ``(customer_id, idempotency_key)`` rolls back the savepoint
        without breaking the outer transaction.
        """
        if now.tzinfo is None:
            raise ValueError("now must be tz-aware")
        row = Execution(
            id=execution_id,
            quote_id=quote_id,
            customer_id=customer_id,
            idempotency_key=idempotency_key,
            status=ExecutionStatus.FAILED.value,
            failure_reason=None,
            response_body={},
            created_at=now,
        )
        session.add(row)
        await session.flush()
        return row

    @staticmethod
    async def get_by_idempotency(
        session: AsyncSession,
        *,
        customer_id: UUID,
        idempotency_key: str,
    ) -> Execution | None:
        stmt = select(Execution).where(
            Execution.customer_id == customer_id,
            Execution.idempotency_key == idempotency_key,
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    async def finalize_succeeded(
        session: AsyncSession,
        execution: Execution,
        *,
        response_body: dict[str, Any],
    ) -> None:
        execution.status = ExecutionStatus.SUCCEEDED.value
        execution.failure_reason = None
        execution.response_body = response_body
        await session.flush()

    @staticmethod
    async def finalize_failed(
        session: AsyncSession,
        execution: Execution,
        *,
        failure_reason: FailureReason,
        response_body: dict[str, Any],
    ) -> None:
        execution.status = ExecutionStatus.FAILED.value
        execution.failure_reason = failure_reason.value
        execution.response_body = response_body
        await session.flush()
