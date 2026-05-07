"""Domain layer -- pure business logic, no I/O.

Importing this package establishes the project-wide Decimal context
(precision 28, ROUND_HALF_EVEN) per SPEC §3. All Decimal arithmetic
in domain code is governed by this context; rounding to currency
minor units happens only at API boundaries via
``Money.quantize_to_minor_units``.

The domain layer must not import from ``app.api``, ``app.infra``, or
``app.observability``. Time, configuration, and persistence are
injected via protocols defined here (``Clock``, etc.) and supplied
at the composition root.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, getcontext

_ctx = getcontext()
_ctx.prec = 28
_ctx.rounding = ROUND_HALF_EVEN
