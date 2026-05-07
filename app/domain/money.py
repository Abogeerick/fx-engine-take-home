"""Money and Balance value objects.

``Money`` is sign-agnostic and used for amounts that may legitimately
be negative (ledger entries, arithmetic intermediates). ``Balance``
is a subclass that enforces the non-negative invariant at construction
time, so type signatures elsewhere can declare ``Balance`` and rely on
it without runtime checks.

Floats are forbidden anywhere in the domain. ``Money`` accepts
``Decimal`` and ``int`` (coerced to Decimal -- the conversion is
exact); it rejects ``float`` and ``bool``. The harm asymmetry is the
point of the no-floats rule: ``float(0.1)`` corrupts, ``int(10)``
does not.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.domain.currency import Currency


@dataclass(frozen=True, slots=True)
class Money:
    amount: Decimal
    currency: Currency

    def __post_init__(self) -> None:
        # bool is a subclass of int -- reject it before the int branch
        # so True/False cannot slip in as 1/0.
        if isinstance(self.amount, bool):
            raise TypeError("Money.amount must be Decimal (bool is not a permitted numeric type)")
        if isinstance(self.amount, int):
            # int -> Decimal is exact; coerce so downstream sees Decimal only.
            object.__setattr__(self, "amount", Decimal(self.amount))
        if not isinstance(self.amount, Decimal):
            raise TypeError(
                "Money.amount must be Decimal or int (no floats permitted); "
                f"got {type(self.amount).__name__}"
            )
        if not isinstance(self.currency, Currency):
            raise TypeError(f"Money.currency must be Currency; got {type(self.currency).__name__}")

    def __add__(self, other: Money) -> Money:
        self._check_same_currency(other)
        return type(self)(amount=self.amount + other.amount, currency=self.currency)

    def __sub__(self, other: Money) -> Money:
        self._check_same_currency(other)
        return type(self)(amount=self.amount - other.amount, currency=self.currency)

    def _check_same_currency(self, other: Money) -> None:
        if not isinstance(other, Money):
            raise TypeError(f"cannot operate on Money and {type(other).__name__}")
        if other.currency is not self.currency:
            raise ValueError(f"currency mismatch: {self.currency.value} vs {other.currency.value}")

    def quantize_to_minor_units(self) -> Money:
        """Round to the currency's minor units using the ambient context.

        The ambient Decimal context is ROUND_HALF_EVEN, set on import of
        ``app.domain``. Use this only at API boundaries (response
        serialization, balance display) -- never mid-computation.
        """
        quantum = Decimal(10) ** -self.currency.minor_units
        return type(self)(
            amount=self.amount.quantize(quantum),
            currency=self.currency,
        )


@dataclass(frozen=True, slots=True)
class Balance(Money):
    def __post_init__(self) -> None:
        # Call Money.__post_init__ by name rather than via super(): the
        # @dataclass(slots=True) decorator replaces the class object,
        # which leaves super()'s __class__ cell pointing at a class
        # that's no longer in the MRO and raises at runtime. See:
        # https://docs.python.org/3/library/dataclasses.html#inheritance
        Money.__post_init__(self)
        if self.amount < 0:
            raise ValueError(
                f"Balance must be non-negative; got {self.amount} {self.currency.value}"
            )
