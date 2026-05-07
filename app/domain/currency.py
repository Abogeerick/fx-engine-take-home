"""Supported ISO 4217 currencies and their minor-unit lookup.

Per SPEC §2: codes are uppercase ISO 4217 strings. Lowercase or
mixed-case input is rejected with a ``ValueError`` rather than
silently normalised, because silent normalisation hides client bugs.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class Currency(StrEnum):
    USD = "USD"
    EUR = "EUR"
    KES = "KES"
    NGN = "NGN"

    @classmethod
    def from_code(cls, code: str) -> Currency:
        """Construct from an uppercase ISO 4217 code.

        Raises ``TypeError`` if ``code`` is not a string, or
        ``ValueError`` if it is empty, not strictly uppercase, or
        not one of the four supported currencies.
        """
        if not isinstance(code, str):
            raise TypeError(f"currency code must be str, got {type(code).__name__}")
        if not code:
            raise ValueError("currency code must not be empty")
        if code != code.upper():
            raise ValueError(f"currency code must be uppercase ISO 4217: got {code!r}")
        try:
            return cls(code)
        except ValueError as exc:
            raise ValueError(f"unsupported currency: {code!r}") from exc

    @property
    def minor_units(self) -> int:
        return _MINOR_UNITS[self]


_MINOR_UNITS: Final[dict[Currency, int]] = {
    Currency.USD: 2,
    Currency.EUR: 2,
    Currency.KES: 2,
    Currency.NGN: 2,
}
