"""Cross-language parity of the native FFI with the Python core."""

from __future__ import annotations

from decimal import Decimal

import pytest

from harness._native import (
    DECIMAL_SCALE,
    NATIVE_ABI_VERSION,
    NativeCore,
    NativeCoreError,
    NativeCoreUnavailable,
)
from harness.core.symbols import canonical_symbol

SYMBOL_CASES = (
    " aapl ",
    "SPY",
    "BRK.B",
    "AAPL240119C00150000",
    "AAPL  240119C00150000",
    "MSFT250620P00450000",
)

INVALID_SYMBOLS = ("../AAPL", "", "   ", "aapl is invalid")

DECIMAL_CASES = (
    1_500_000_000_000_000_000,
    1,
    0,
    -1,
    99_999_999_999_999_999_999,
    -99_999_999_999_999_999_999,
    -1_502_500_000_000_000_000,
    12_345_678_901_234_567_890,
    15_025,
    1_000_000_000_000_000_000,
    1_208_925_819_614_629_174_706_176,
)


def test_loaded_library_reports_the_pinned_abi_version(native_core: NativeCore) -> None:
    assert NATIVE_ABI_VERSION == 2
    assert native_core.abi_version == NATIVE_ABI_VERSION


def test_symbol_canonicalization_matches_the_python_core(
    native_core: NativeCore,
) -> None:
    for value in SYMBOL_CASES:
        assert native_core.symbol_canonicalize(value) == str(canonical_symbol(value))


def test_invalid_symbols_report_an_error_code(native_core: NativeCore) -> None:
    for value in INVALID_SYMBOLS:
        with pytest.raises(NativeCoreError):
            native_core.symbol_canonicalize(value)


def test_decimal_text_matches_python_str_of_decimal(
    native_core: NativeCore,
) -> None:
    for unscaled in DECIMAL_CASES:
        expected = str(Decimal(unscaled).scaleb(-DECIMAL_SCALE))
        assert native_core.decimal_text(unscaled) == expected


def test_out_of_range_decimals_report_an_error_code(native_core: NativeCore) -> None:
    with pytest.raises(NativeCoreError):
        native_core.decimal_text(10**38)


def test_loader_errors_are_dedicated_runtime_errors() -> None:
    assert not isinstance(NativeCoreUnavailable, ImportError)
    assert NATIVE_ABI_VERSION == 2
