"""NumPy facade over the native CRR quant kernels.

The kernels live in ``src/harness/native/quant.zig`` behind the versioned C
ABI loaded by :mod:`harness._native`; this module is the supported Python
surface. Conventions:

- Time to expiry is in years; rates, dividend yields, and volatilities are
  continuously compounded annuals; theta is returned per year.
- ``is_call`` and ``is_american`` accept booleans or 0/1 values, scalars or
  arrays, and broadcast against the numeric inputs.
- Inputs may be scalars, sequences, NumPy arrays, or ``decimal.Decimal``
  values; everything broadcasts to one common shape and converts to
  contiguous ``float64`` buffers for the native batch calls.
- Invalid elements (non-positive spot, strike, or volatility, negative time,
  step counts outside ``[2, native_max_steps()]``, failed bisection
  brackets) come back as NaN rather than raising; only structural problems
  (a missing or stale native core) raise.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from typing import Any, Final, Literal

import numpy as np

from harness._native import NativeCore, load_native_core

OptionType = Literal["call", "put"]
ExerciseStyle = Literal["european", "american"]

Number = float | Decimal

DEFAULT_STEPS: Final = 128
DEFAULT_BISECT_ITERATIONS: Final = 64

SECONDS_PER_YEAR: Final = 365.0 * 24.0 * 60.0 * 60.0

_DOUBLE_P = ctypes.POINTER(ctypes.c_double)
_UBYTE_P = ctypes.POINTER(ctypes.c_ubyte)
_UINT32_P = ctypes.POINTER(ctypes.c_uint32)


@dataclass(frozen=True)
class CrrQuote:
    """Scalar CRR quote with tree greeks (theta per year, vega per vol)."""

    price: float
    delta: float
    gamma: float
    theta: float
    vega: float


@dataclass(frozen=True)
class CrrBatchResult:
    """Broadcast batch outputs; each field is a ``float64`` ndarray."""

    price: np.ndarray
    delta: np.ndarray
    gamma: np.ndarray
    theta: np.ndarray
    vega: np.ndarray


@lru_cache(maxsize=1)
def _core() -> NativeCore:
    return load_native_core()


def native_max_steps() -> int:
    """Return the largest binomial step count the native kernels accept."""
    return _core().quant_max_steps()


def _f64(values: object, shape: tuple[int, ...], name: str) -> np.ndarray:
    try:
        broadcast = np.broadcast_to(np.asarray(values, dtype=np.float64), shape)
    except (TypeError, ValueError) as error:
        raise TypeError(f"{name} must broadcast to numeric float64") from error
    return np.ascontiguousarray(broadcast)


def _flags(is_call: object, is_american: object, shape: tuple[int, ...]) -> np.ndarray:
    call = _f64(np.asarray(is_call) != 0, shape, "is_call")
    american = _f64(np.asarray(is_american) != 0, shape, "is_american")
    return np.ascontiguousarray(
        call.astype(np.uint8) | (american.astype(np.uint8) << 1)
    )


def _steps(steps: object, shape: tuple[int, ...]) -> np.ndarray:
    raw = np.asarray(steps)
    if raw.dtype.kind not in "iu":
        raise TypeError("steps must be an integer scalar or array")
    return np.ascontiguousarray(
        np.broadcast_to(raw.astype(np.uint32), shape), dtype=np.uint32
    )


def _size(shape: tuple[int, ...]) -> int:
    return int(np.prod(shape, dtype=np.int64)) if shape else 1


def _pointer(array: np.ndarray, pointer_type: type[Any]) -> Any:
    return array.ctypes.data_as(pointer_type)


def _common_shape(*values: Any) -> tuple[int, ...]:
    shapes = (np.shape(value) for value in values)
    return tuple(np.broadcast_shapes(*shapes))


def crr_price_batch(
    *,
    s: object,
    k: object,
    t: object,
    r: object,
    q: object,
    sigma: object,
    is_call: object,
    is_american: object,
    steps: object = DEFAULT_STEPS,
) -> CrrBatchResult:
    """Batch CRR prices with tree greeks from the native core."""
    shape = _common_shape(s, k, t, r, q, sigma, is_call, is_american, steps)
    s_a = _f64(s, shape, "s")
    k_a = _f64(k, shape, "k")
    t_a = _f64(t, shape, "t")
    r_a = _f64(r, shape, "r")
    q_a = _f64(q, shape, "q")
    sigma_a = _f64(sigma, shape, "sigma")
    flags_a = _flags(is_call, is_american, shape)
    steps_a = _steps(steps, shape)
    count = _size(shape)
    out_price = np.empty(shape, dtype=np.float64)
    out_delta = np.empty(shape, dtype=np.float64)
    out_gamma = np.empty(shape, dtype=np.float64)
    out_theta = np.empty(shape, dtype=np.float64)
    out_vega = np.empty(shape, dtype=np.float64)
    _core().quant_crr_price_batch(
        _pointer(s_a, _DOUBLE_P),
        _pointer(k_a, _DOUBLE_P),
        _pointer(t_a, _DOUBLE_P),
        _pointer(r_a, _DOUBLE_P),
        _pointer(q_a, _DOUBLE_P),
        _pointer(sigma_a, _DOUBLE_P),
        _pointer(flags_a, _UBYTE_P),
        _pointer(steps_a, _UINT32_P),
        count,
        _pointer(out_price, _DOUBLE_P),
        _pointer(out_delta, _DOUBLE_P),
        _pointer(out_gamma, _DOUBLE_P),
        _pointer(out_theta, _DOUBLE_P),
        _pointer(out_vega, _DOUBLE_P),
    )
    return CrrBatchResult(
        price=out_price,
        delta=out_delta,
        gamma=out_gamma,
        theta=out_theta,
        vega=out_vega,
    )


def crr_implied_time_batch(
    *,
    s: object,
    k: object,
    r: object,
    q: object,
    sigma: object,
    price: object,
    t_lower: object,
    t_upper: object,
    is_call: object,
    is_american: object,
    steps: object = DEFAULT_STEPS,
    bisect_iterations: int = DEFAULT_BISECT_ITERATIONS,
) -> np.ndarray:
    """Batch implied time to expiry: solve CRR(S, K, T) = price for T."""
    shape = _common_shape(
        s, k, r, q, sigma, price, t_lower, t_upper, is_call, is_american, steps
    )
    s_a = _f64(s, shape, "s")
    k_a = _f64(k, shape, "k")
    r_a = _f64(r, shape, "r")
    q_a = _f64(q, shape, "q")
    sigma_a = _f64(sigma, shape, "sigma")
    target_a = _f64(price, shape, "price")
    t_lower_a = _f64(t_lower, shape, "t_lower")
    t_upper_a = _f64(t_upper, shape, "t_upper")
    flags_a = _flags(is_call, is_american, shape)
    steps_a = _steps(steps, shape)
    out_t = np.empty(shape, dtype=np.float64)
    _core().quant_crr_implied_time_batch(
        _pointer(s_a, _DOUBLE_P),
        _pointer(k_a, _DOUBLE_P),
        _pointer(r_a, _DOUBLE_P),
        _pointer(q_a, _DOUBLE_P),
        _pointer(sigma_a, _DOUBLE_P),
        _pointer(flags_a, _UBYTE_P),
        _pointer(steps_a, _UINT32_P),
        _pointer(target_a, _DOUBLE_P),
        _pointer(t_lower_a, _DOUBLE_P),
        _pointer(t_upper_a, _DOUBLE_P),
        _size(shape),
        bisect_iterations,
        _pointer(out_t, _DOUBLE_P),
    )
    return out_t


def crr_implied_underlying_batch(
    *,
    k: object,
    t: object,
    r: object,
    q: object,
    sigma: object,
    price: object,
    s_lower: object,
    s_upper: object,
    is_call: object,
    is_american: object,
    steps: object = DEFAULT_STEPS,
    bisect_iterations: int = DEFAULT_BISECT_ITERATIONS,
) -> np.ndarray:
    """Batch implied underlying spot: solve CRR(S, K, T) = price for S."""
    shape = _common_shape(
        k, t, r, q, sigma, price, s_lower, s_upper, is_call, is_american, steps
    )
    k_a = _f64(k, shape, "k")
    t_a = _f64(t, shape, "t")
    r_a = _f64(r, shape, "r")
    q_a = _f64(q, shape, "q")
    sigma_a = _f64(sigma, shape, "sigma")
    target_a = _f64(price, shape, "price")
    s_lower_a = _f64(s_lower, shape, "s_lower")
    s_upper_a = _f64(s_upper, shape, "s_upper")
    flags_a = _flags(is_call, is_american, shape)
    steps_a = _steps(steps, shape)
    out_s = np.empty(shape, dtype=np.float64)
    _core().quant_crr_implied_underlying_batch(
        _pointer(k_a, _DOUBLE_P),
        _pointer(t_a, _DOUBLE_P),
        _pointer(r_a, _DOUBLE_P),
        _pointer(q_a, _DOUBLE_P),
        _pointer(sigma_a, _DOUBLE_P),
        _pointer(flags_a, _UBYTE_P),
        _pointer(steps_a, _UINT32_P),
        _pointer(target_a, _DOUBLE_P),
        _pointer(s_lower_a, _DOUBLE_P),
        _pointer(s_upper_a, _DOUBLE_P),
        _size(shape),
        bisect_iterations,
        _pointer(out_s, _DOUBLE_P),
    )
    return out_s


def _exercise_flag(exercise: ExerciseStyle) -> bool:
    if exercise == "american":
        return True
    if exercise == "european":
        return False
    raise ValueError("exercise must be 'european' or 'american'")


def _option_flag(option_type: OptionType) -> bool:
    if option_type == "call":
        return True
    if option_type == "put":
        return False
    raise ValueError("option_type must be 'call' or 'put'")


def crr_price(
    *,
    s: Number,
    k: Number,
    t: Number,
    r: Number,
    q: Number,
    sigma: Number,
    option_type: OptionType,
    exercise: ExerciseStyle,
    steps: int = DEFAULT_STEPS,
) -> CrrQuote:
    """Scalar CRR quote with tree greeks."""
    result = crr_price_batch(
        s=s,
        k=k,
        t=t,
        r=r,
        q=q,
        sigma=sigma,
        is_call=_option_flag(option_type),
        is_american=_exercise_flag(exercise),
        steps=steps,
    )
    return CrrQuote(
        price=float(result.price),
        delta=float(result.delta),
        gamma=float(result.gamma),
        theta=float(result.theta),
        vega=float(result.vega),
    )


def crr_implied_time(
    *,
    s: Number,
    k: Number,
    r: Number,
    q: Number,
    sigma: Number,
    price: Number,
    t_lower: Number,
    t_upper: Number,
    option_type: OptionType,
    exercise: ExerciseStyle,
    steps: int = DEFAULT_STEPS,
    bisect_iterations: int = DEFAULT_BISECT_ITERATIONS,
) -> float:
    """Scalar implied time to expiry from a CRR price."""
    solved = crr_implied_time_batch(
        s=s,
        k=k,
        r=r,
        q=q,
        sigma=sigma,
        price=price,
        t_lower=t_lower,
        t_upper=t_upper,
        is_call=_option_flag(option_type),
        is_american=_exercise_flag(exercise),
        steps=steps,
        bisect_iterations=bisect_iterations,
    )
    return float(solved)


def crr_implied_underlying(
    *,
    k: Number,
    t: Number,
    r: Number,
    q: Number,
    sigma: Number,
    price: Number,
    s_lower: Number,
    s_upper: Number,
    option_type: OptionType,
    exercise: ExerciseStyle,
    steps: int = DEFAULT_STEPS,
    bisect_iterations: int = DEFAULT_BISECT_ITERATIONS,
) -> float:
    """Scalar implied underlying spot from a CRR price."""
    solved = crr_implied_underlying_batch(
        k=k,
        t=t,
        r=r,
        q=q,
        sigma=sigma,
        price=price,
        s_lower=s_lower,
        s_upper=s_upper,
        is_call=_option_flag(option_type),
        is_american=_exercise_flag(exercise),
        steps=steps,
        bisect_iterations=bisect_iterations,
    )
    return float(solved)
