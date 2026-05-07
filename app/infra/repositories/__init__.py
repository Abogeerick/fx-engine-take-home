"""Repositories -- one class per aggregate, methods take a session.

Repositories own *what* SQL runs, not *when* a session opens or
commits. The execute path in step 3 manages its own transaction
explicitly via ``SELECT ... FOR UPDATE`` semantics; simpler reads
in the API layer use ``session_scope`` from ``app.infra.db``.
"""

from app.infra.repositories.balance import BalanceRepository, InsufficientBalance
from app.infra.repositories.customer import CustomerRepository
from app.infra.repositories.rate import RateRepository

__all__ = [
    "BalanceRepository",
    "CustomerRepository",
    "InsufficientBalance",
    "RateRepository",
]
