"""Behavior of the harness.quant facade over the native CRR kernels."""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any, cast

import numpy as np
import pytest

from harness._native import NativeCore
from harness.quant import (
    CrrQuote,
    crr_implied_time,
    crr_implied_time_batch,
    crr_implied_underlying,
    crr_implied_underlying_batch,
    crr_price,
    crr_price_batch,
    native_max_steps,
)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black_scholes(
    s: float, k: float, t: float, r: float, q: float, sigma: float, call: bool
) -> float:
    root_t = math.sqrt(t)
    d1 = (math.log(s / k) + (r - q + sigma * sigma / 2) * t) / (sigma * root_t)
    d2 = d1 - sigma * root_t
    if call:
        return s * math.exp(-q * t) * norm_cdf(d1) - k * math.exp(-r * t) * norm_cdf(d2)
    return k * math.exp(-r * t) * norm_cdf(-d2) - s * math.exp(-q * t) * norm_cdf(-d1)


def test_native_max_steps_matches_the_pinned_kernel(native_core: NativeCore) -> None:
    assert native_core.quant_max_steps() == native_max_steps() == 1024


def test_batch_converges_to_black_scholes_for_calls_and_puts(
    native_core: NativeCore,
) -> None:
    result = crr_price_batch(
        s=np.array([450.0, 455.0, 460.0]),
        k=450.0,
        t=2.0 / 365.0,
        r=0.05,
        q=0.012,
        sigma=np.array([0.15, 0.32, 0.08]),
        is_call=np.array([True, False, True]),
        is_american=False,
        steps=512,
    )
    expected = [
        black_scholes(450.0, 450.0, 2.0 / 365.0, 0.05, 0.012, 0.15, True),
        black_scholes(455.0, 450.0, 2.0 / 365.0, 0.05, 0.012, 0.32, False),
        black_scholes(460.0, 450.0, 2.0 / 365.0, 0.05, 0.012, 0.08, True),
    ]
    for actual, reference in zip(result.price, expected, strict=True):
        assert abs(float(actual) - reference) < 2e-3


def test_european_put_call_parity_through_the_batch(native_core: NativeCore) -> None:
    call = crr_price_batch(
        s=470.0,
        k=465.0,
        t=3.0 / 365.0,
        r=0.045,
        q=0.013,
        sigma=0.21,
        is_call=True,
        is_american=False,
        steps=400,
    )
    put = crr_price_batch(
        s=470.0,
        k=465.0,
        t=3.0 / 365.0,
        r=0.045,
        q=0.013,
        sigma=0.21,
        is_call=False,
        is_american=False,
        steps=400,
    )
    parity = 470.0 * math.exp(-0.013 * 3.0 / 365.0) - 465.0 * math.exp(
        -0.045 * 3.0 / 365.0
    )
    assert abs((float(call.price) - float(put.price)) - parity) < 1e-8


def test_american_put_dominates_european_put(native_core: NativeCore) -> None:
    european = crr_price(
        s=90.0,
        k=100.0,
        t=30.0 / 365.0,
        r=0.05,
        q=0.0,
        sigma=0.25,
        option_type="put",
        exercise="european",
        steps=256,
    )
    american = crr_price(
        s=90.0,
        k=100.0,
        t=30.0 / 365.0,
        r=0.05,
        q=0.0,
        sigma=0.25,
        option_type="put",
        exercise="american",
        steps=256,
    )
    assert isinstance(american, CrrQuote)
    assert american.price >= european.price
    assert american.price >= 10.0


def test_tree_greeks_stay_in_bounds(native_core: NativeCore) -> None:
    quote = crr_price(
        s=500.0,
        k=500.0,
        t=1.0 / 365.0,
        r=0.04,
        q=0.0,
        sigma=0.30,
        option_type="call",
        exercise="american",
    )
    assert quote.theta < 0
    assert 0 < quote.delta < 1
    assert quote.gamma > 0
    assert quote.vega > 0


def test_zero_expiry_returns_the_intrinsic_value(native_core: NativeCore) -> None:
    result = crr_price_batch(
        s=np.array([105.0, 95.0]),
        k=100.0,
        t=0.0,
        r=0.04,
        q=0.0,
        sigma=0.20,
        is_call=np.array([True, False]),
        is_american=True,
    )
    assert result.price.tolist() == [5.0, 5.0]
    assert result.delta.tolist() == [1.0, -1.0]


def test_invalid_elements_come_back_as_nan(native_core: NativeCore) -> None:
    result = crr_price_batch(
        s=np.array([100.0, 100.0, -100.0]),
        k=100.0,
        t=0.01,
        r=0.04,
        q=0.0,
        sigma=np.array([0.0, 0.2, 0.2]),
        is_call=True,
        is_american=False,
        steps=np.array([1, 64, 64]),
    )
    assert math.isnan(float(result.price[0]))
    assert math.isnan(float(result.price[2]))
    assert math.isfinite(float(result.price[1]))
    assert math.isfinite(float(result.delta[1]))


def test_scalar_quote_matches_the_batch_element(native_core: NativeCore) -> None:
    batch = crr_price_batch(
        s=502.5,
        k=500.0,
        t=1.0 / 48.0,
        r=0.045,
        q=0.012,
        sigma=0.27,
        is_call=True,
        is_american=True,
        steps=96,
    )
    scalar = crr_price(
        s=502.5,
        k=500.0,
        t=1.0 / 48.0,
        r=0.045,
        q=0.012,
        sigma=0.27,
        option_type="call",
        exercise="american",
        steps=96,
    )
    assert scalar.price == float(batch.price)
    assert scalar.delta == float(batch.delta)
    assert scalar.gamma == float(batch.gamma)
    assert scalar.theta == float(batch.theta)
    assert scalar.vega == float(batch.vega)


def test_decimal_inputs_are_accepted(native_core: NativeCore) -> None:
    decimal_quote = crr_price(
        s=float(Decimal("502.50")),
        k=Decimal("500"),
        t=1.0 / 48.0,
        r=Decimal("0.045"),
        q=0.0,
        sigma=Decimal("0.27"),
        option_type="call",
        exercise="american",
        steps=96,
    )
    float_quote = crr_price(
        s=502.5,
        k=500.0,
        t=1.0 / 48.0,
        r=0.045,
        q=0.0,
        sigma=0.27,
        option_type="call",
        exercise="american",
        steps=96,
    )
    assert decimal_quote.price == float_quote.price


def test_implied_time_round_trip(native_core: NativeCore) -> None:
    target_t = 4.0 / (365.0 * 24.0)
    target_price = crr_price(
        s=500.0,
        k=502.0,
        t=target_t,
        r=0.04,
        q=0.0,
        sigma=0.35,
        option_type="call",
        exercise="american",
        steps=96,
    ).price
    solved = crr_implied_time(
        s=500.0,
        k=502.0,
        r=0.04,
        q=0.0,
        sigma=0.35,
        price=target_price,
        t_lower=target_t / 4.0,
        t_upper=target_t * 4.0,
        option_type="call",
        exercise="american",
        steps=96,
    )
    assert math.isfinite(solved)
    assert abs(solved - target_t) < 1e-9


def test_implied_time_batch_round_trip_and_failed_bracket(
    native_core: NativeCore,
) -> None:
    target_t = 2.0 / (365.0 * 24.0)
    prices = crr_price_batch(
        s=500.0,
        k=498.0,
        t=target_t,
        r=0.04,
        q=0.0,
        sigma=0.28,
        is_call=True,
        is_american=True,
        steps=96,
    ).price
    solved = crr_implied_time_batch(
        s=500.0,
        k=498.0,
        r=0.04,
        q=0.0,
        sigma=0.28,
        price=prices,
        t_lower=target_t / 8.0,
        t_upper=target_t * 8.0,
        is_call=True,
        is_american=True,
        steps=96,
    )
    assert abs(float(solved) - target_t) < 1e-9

    impossible = crr_implied_time_batch(
        s=500.0,
        k=498.0,
        r=0.04,
        q=0.0,
        sigma=0.28,
        price=1e6,
        t_lower=1.0 / (365.0 * 24.0),
        t_upper=1.0,
        is_call=True,
        is_american=True,
        steps=96,
    )
    assert math.isnan(float(impossible))


def test_implied_underlying_round_trip(native_core: NativeCore) -> None:
    target_s = 503.75
    target_price = crr_price(
        s=target_s,
        k=500.0,
        t=1.0 / 48.0,
        r=0.04,
        q=0.0,
        sigma=0.30,
        option_type="put",
        exercise="american",
        steps=96,
    ).price
    solved = crr_implied_underlying(
        k=500.0,
        t=1.0 / 48.0,
        r=0.04,
        q=0.0,
        sigma=0.30,
        price=target_price,
        s_lower=target_s * 0.5,
        s_upper=target_s * 1.5,
        option_type="put",
        exercise="american",
        steps=96,
    )
    assert math.isfinite(solved)
    assert abs(solved - target_s) < 1e-6


def test_implied_underlying_batch_handles_puts_and_calls(
    native_core: NativeCore,
) -> None:
    prices = crr_price_batch(
        s=np.array([498.0, 502.0]),
        k=500.0,
        t=1.0 / 96.0,
        r=0.04,
        q=0.0,
        sigma=0.25,
        is_call=np.array([False, True]),
        is_american=True,
        steps=96,
    ).price
    solved = crr_implied_underlying_batch(
        k=500.0,
        t=1.0 / 96.0,
        r=0.04,
        q=0.0,
        sigma=0.25,
        price=prices,
        s_lower=480.0,
        s_upper=520.0,
        is_call=np.array([False, True]),
        is_american=True,
        steps=96,
    )
    assert solved.shape == (2,)
    assert abs(float(solved[0]) - 498.0) < 1e-6
    assert abs(float(solved[1]) - 502.0) < 1e-6


def test_invalid_option_type_and_exercise_raise(native_core: NativeCore) -> None:
    bad_type = cast(Any, "straddle")
    bad_exercise = cast(Any, "exotic")
    with pytest.raises(ValueError, match="option_type"):
        crr_price(
            s=500.0,
            k=500.0,
            t=0.01,
            r=0.04,
            q=0.0,
            sigma=0.3,
            option_type=bad_type,
            exercise="american",
        )
    with pytest.raises(ValueError, match="exercise"):
        crr_price(
            s=500.0,
            k=500.0,
            t=0.01,
            r=0.04,
            q=0.0,
            sigma=0.3,
            option_type="call",
            exercise=bad_exercise,
        )


def test_non_integer_steps_are_rejected(native_core: NativeCore) -> None:
    with pytest.raises(TypeError, match="steps"):
        crr_price_batch(
            s=500.0,
            k=500.0,
            t=0.01,
            r=0.04,
            q=0.0,
            sigma=0.3,
            is_call=True,
            is_american=False,
            steps=12.5,
        )
