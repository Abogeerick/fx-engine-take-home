"""Unit tests for the Currency enum and its constructor classmethod."""

from __future__ import annotations

import pytest

from app.domain.currency import Currency


class TestFromCode:
    def test_uppercase_iso_code_returns_member(self) -> None:
        assert Currency.from_code("USD") is Currency.USD
        assert Currency.from_code("EUR") is Currency.EUR
        assert Currency.from_code("KES") is Currency.KES
        assert Currency.from_code("NGN") is Currency.NGN

    @pytest.mark.parametrize("code", ["usd", "Usd", "uSd", "USd", "uSD"])
    def test_non_uppercase_rejected(self, code: str) -> None:
        with pytest.raises(ValueError, match="uppercase"):
            Currency.from_code(code)

    @pytest.mark.parametrize("code", ["XYZ", "GBP", "JPY", "ABC"])
    def test_unknown_uppercase_code_rejected(self, code: str) -> None:
        with pytest.raises(ValueError, match="unsupported"):
            Currency.from_code(code)

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(ValueError):
            Currency.from_code("")

    def test_non_string_rejected(self) -> None:
        with pytest.raises(TypeError):
            Currency.from_code(123)  # type: ignore[arg-type]


class TestMinorUnits:
    def test_all_supported_currencies_have_two_minor_units(self) -> None:
        for c in Currency:
            assert c.minor_units == 2


class TestEnumeration:
    def test_exactly_four_currencies(self) -> None:
        assert {c.value for c in Currency} == {"USD", "EUR", "KES", "NGN"}
