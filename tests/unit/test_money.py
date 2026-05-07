"""Unit tests for Money/Balance value objects and the Decimal context."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import ROUND_HALF_EVEN, Decimal, getcontext

import pytest

import app.domain  # noqa: F401  -- import establishes the Decimal context per SPEC §3
from app.domain.currency import Currency
from app.domain.money import Balance, Money


class TestDecimalContext:
    def test_precision_is_28(self) -> None:
        assert getcontext().prec == 28

    def test_rounding_is_half_even(self) -> None:
        assert getcontext().rounding == ROUND_HALF_EVEN


class TestConstruction:
    def test_decimal_amount_accepted(self) -> None:
        m = Money(amount=Decimal("10.00"), currency=Currency.USD)
        assert m.amount == Decimal("10.00")
        assert m.currency is Currency.USD

    def test_float_amount_rejected(self) -> None:
        with pytest.raises(TypeError, match="Decimal"):
            Money(amount=10.0, currency=Currency.USD)  # type: ignore[arg-type]

    def test_int_amount_accepted_and_coerced(self) -> None:
        # int -> Decimal is exact; we coerce rather than reject so that
        # Hypothesis-generated integers are ergonomic in property tests.
        m = Money(amount=10, currency=Currency.USD)  # type: ignore[arg-type]
        assert m.amount == Decimal(10)
        assert isinstance(m.amount, Decimal)

    def test_bool_amount_rejected(self) -> None:
        # bool is a subclass of int; rejecting it explicitly prevents
        # True/False from being accepted as 1/0.
        with pytest.raises(TypeError, match="bool"):
            Money(amount=True, currency=Currency.USD)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="bool"):
            Money(amount=False, currency=Currency.USD)  # type: ignore[arg-type]

    def test_non_currency_rejected(self) -> None:
        with pytest.raises(TypeError):
            Money(amount=Decimal("1"), currency="USD")  # type: ignore[arg-type]

    def test_immutable(self) -> None:
        m = Money(amount=Decimal("1.00"), currency=Currency.USD)
        with pytest.raises(FrozenInstanceError):
            m.amount = Decimal("2.00")  # type: ignore[misc]

    def test_equality(self) -> None:
        a = Money(amount=Decimal("1.00"), currency=Currency.USD)
        b = Money(amount=Decimal("1.00"), currency=Currency.USD)
        assert a == b


class TestArithmetic:
    def test_same_currency_add(self) -> None:
        a = Money(amount=Decimal("10.00"), currency=Currency.USD)
        b = Money(amount=Decimal("5.50"), currency=Currency.USD)
        assert (a + b).amount == Decimal("15.50")

    def test_same_currency_sub(self) -> None:
        a = Money(amount=Decimal("10.00"), currency=Currency.USD)
        b = Money(amount=Decimal("3.25"), currency=Currency.USD)
        assert (a - b).amount == Decimal("6.75")

    def test_money_sub_can_go_negative(self) -> None:
        a = Money(amount=Decimal("1.00"), currency=Currency.USD)
        b = Money(amount=Decimal("3.00"), currency=Currency.USD)
        assert (a - b).amount == Decimal("-2.00")

    def test_cross_currency_add_raises(self) -> None:
        a = Money(amount=Decimal("10.00"), currency=Currency.USD)
        b = Money(amount=Decimal("5.00"), currency=Currency.EUR)
        with pytest.raises(ValueError, match="currency mismatch"):
            _ = a + b

    def test_cross_currency_sub_raises(self) -> None:
        a = Money(amount=Decimal("10.00"), currency=Currency.USD)
        b = Money(amount=Decimal("5.00"), currency=Currency.KES)
        with pytest.raises(ValueError, match="currency mismatch"):
            _ = a - b

    def test_add_with_non_money_raises(self) -> None:
        a = Money(amount=Decimal("10.00"), currency=Currency.USD)
        with pytest.raises(TypeError):
            _ = a + 5  # type: ignore[operator]


class TestQuantize:
    def test_rounds_down_when_below_half(self) -> None:
        m = Money(amount=Decimal("12.344"), currency=Currency.USD)
        assert m.quantize_to_minor_units().amount == Decimal("12.34")

    def test_rounds_up_when_above_half(self) -> None:
        m = Money(amount=Decimal("12.346"), currency=Currency.USD)
        assert m.quantize_to_minor_units().amount == Decimal("12.35")

    def test_banker_rounding_half_to_even_up(self) -> None:
        # 0.135 -> 0.14: digit before rounding (3) is odd, so we round to 4 (even).
        m = Money(amount=Decimal("0.135"), currency=Currency.USD)
        assert m.quantize_to_minor_units().amount == Decimal("0.14")

    def test_banker_rounding_half_to_even_down(self) -> None:
        # 0.125 -> 0.12: digit before rounding (2) is even, stays at 2.
        m = Money(amount=Decimal("0.125"), currency=Currency.USD)
        assert m.quantize_to_minor_units().amount == Decimal("0.12")

    def test_quantize_preserves_currency(self) -> None:
        m = Money(amount=Decimal("1.234567"), currency=Currency.KES)
        q = m.quantize_to_minor_units()
        assert q.currency is Currency.KES


class TestBalance:
    def test_zero_balance_allowed(self) -> None:
        b = Balance(amount=Decimal("0.00"), currency=Currency.USD)
        assert b.amount == Decimal("0.00")

    def test_positive_balance_allowed(self) -> None:
        b = Balance(amount=Decimal("100.00"), currency=Currency.USD)
        assert b.amount == Decimal("100.00")

    def test_negative_balance_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            Balance(amount=Decimal("-0.01"), currency=Currency.USD)

    def test_subtraction_below_zero_raises(self) -> None:
        a = Balance(amount=Decimal("1.00"), currency=Currency.USD)
        b = Balance(amount=Decimal("3.00"), currency=Currency.USD)
        with pytest.raises(ValueError, match="non-negative"):
            _ = a - b

    def test_balance_addition_returns_balance(self) -> None:
        a = Balance(amount=Decimal("10.00"), currency=Currency.USD)
        b = Balance(amount=Decimal("5.00"), currency=Currency.USD)
        result = a + b
        assert isinstance(result, Balance)
        assert result.amount == Decimal("15.00")
