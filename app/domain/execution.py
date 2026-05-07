"""Execution status + failure-reason enums.

Status maps to the persisted ``status`` column. ``FailureReason``
classifies *why* a status='failed' execution failed, so retries
can return a sticky response and observability can group failures
by cause.
"""

from __future__ import annotations

from enum import StrEnum


class ExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class FailureReason(StrEnum):
    INSUFFICIENT_BALANCE = "insufficient_balance"
    QUOTE_EXPIRED = "quote_expired"
    QUOTE_OWNERSHIP_MISMATCH = "quote_ownership_mismatch"
    QUOTE_ALREADY_CONSUMED = "quote_already_consumed"
    IDEMPOTENCY_KEY_REUSED = "idempotency_key_reused"
