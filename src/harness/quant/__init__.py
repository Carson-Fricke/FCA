"""Public quant facade for the quant-harness.

Batch quantitative-analysis kernels over the native core, currently the
Cox-Ross-Rubinstein binomial pricer with tree greeks and implied-value
inversions. See ``src/harness/native/quant.zig`` for the kernel semantics and
``.github/architecture/repository.md`` for the package boundary.
"""

from harness.quant.pricing import (
    DEFAULT_BISECT_ITERATIONS,
    DEFAULT_STEPS,
    SECONDS_PER_YEAR,
    CrrBatchResult,
    CrrQuote,
    ExerciseStyle,
    OptionType,
    crr_implied_time,
    crr_implied_time_batch,
    crr_implied_underlying,
    crr_implied_underlying_batch,
    crr_price,
    crr_price_batch,
    native_max_steps,
)

__all__ = [
    "DEFAULT_BISECT_ITERATIONS",
    "DEFAULT_STEPS",
    "CrrBatchResult",
    "CrrQuote",
    "ExerciseStyle",
    "OptionType",
    "SECONDS_PER_YEAR",
    "crr_implied_time",
    "crr_implied_time_batch",
    "crr_implied_underlying",
    "crr_implied_underlying_batch",
    "crr_price",
    "crr_price_batch",
    "native_max_steps",
]
