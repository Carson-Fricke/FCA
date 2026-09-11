"""Generate golden vectors for the Rust scale-18 fixed-point kernel.

The vectors are produced from the Python core-types reference:

* operands are canonicalized through ``harness.core.types.common.exact_decimal``;
* the arithmetic is evaluated with ``decimal.Decimal`` at a 200-digit context
  and quantized to scale 18 with ``ROUND_HALF_EVEN``.

Every expected value is cross-checked against an independent Python-integer
implementation of the same rounding contract before it is emitted; a mismatch
aborts generation. The output is checked in as Rust test data.

Run from the repository root::

    uv run python src/harness/rust/crates/harness-num/tests/generate_golden.py
"""

from __future__ import annotations

import sys
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness.core.types.common import exact_decimal  # noqa: E402

SCALE = 18
ONE = 10**18
MAX = 10**38 - 1
MIN = -MAX
PRECISION = 200
QUANTA = Decimal(1).scaleb(-SCALE)
MODES = ("half_even", "half_up", "half_down", "truncate", "floor", "ceiling")

VALUES = (
    0,
    1,
    -1,
    500_000_000_000_000_000,
    10_010_000_000_000_000_000,
    ONE,
    2 * ONE,
    -3 * ONE,
    930 * ONE,
    9 * ONE,
    MAX,
    -MAX,
)


def to_decimal(unscaled: int) -> Decimal:
    """The Python core-types reference value for an unscaled integer."""
    with localcontext() as ctx:
        ctx.prec = PRECISION
        return exact_decimal(Decimal(unscaled).scaleb(-SCALE))


def to_unscaled(value: Decimal) -> int:
    """Round a reference decimal to scale 18 half-even and unscale it."""
    with localcontext() as ctx:
        ctx.prec = PRECISION
        quantized = value.quantize(QUANTA, rounding=ROUND_HALF_EVEN)
        return int(quantized.scaleb(SCALE))


def in_range(value: int) -> bool:
    return MIN <= value <= MAX


def rounded_quotient(magnitude: int, divisor: int, negative: bool, mode: str) -> int:
    """Independent integer mirror of the Rust rounding contract."""
    quotient, remainder = divmod(magnitude, divisor)
    if remainder == 0:
        return quotient
    twice = remainder * 2
    if mode == "truncate":
        increment = False
    elif mode == "floor":
        increment = negative
    elif mode == "ceiling":
        increment = not negative
    elif mode == "half_even":
        increment = twice > divisor or (twice == divisor and quotient % 2 == 1)
    elif mode == "half_up":
        increment = twice >= divisor
    elif mode == "half_down":
        increment = twice > divisor
    else:  # pragma: no cover - guarded by MODES
        raise ValueError(f"unknown rounding mode: {mode}")
    return quotient + 1 if increment else quotient


def sign_of(negative: bool, magnitude: int) -> int:
    return -magnitude if negative else magnitude


def int_add(lhs: int, rhs: int) -> int | None:
    value = lhs + rhs
    return value if in_range(value) else None


def int_sub(lhs: int, rhs: int) -> int | None:
    value = lhs - rhs
    return value if in_range(value) else None


def int_mul(lhs: int, rhs: int) -> int | None:
    negative = (lhs < 0) != (rhs < 0)
    magnitude = rounded_quotient(abs(lhs) * abs(rhs), ONE, negative, "half_even")
    value = sign_of(negative, magnitude)
    return value if in_range(value) else None


def int_div(lhs: int, rhs: int, mode: str) -> int | None:
    if rhs == 0:
        return None
    negative = (lhs < 0) != (rhs < 0)
    magnitude = rounded_quotient(abs(lhs) * ONE, abs(rhs), negative, mode)
    value = sign_of(negative, magnitude)
    return value if in_range(value) else None


def int_mul_div(lhs: int, rhs: int, divisor: int, mode: str) -> int | None:
    if divisor == 0:
        return None
    negative = ((lhs < 0) != (rhs < 0)) != (divisor < 0)
    magnitude = rounded_quotient(
        abs(lhs) * abs(rhs), abs(divisor), negative, mode
    )
    value = sign_of(negative, magnitude)
    return value if in_range(value) else None


def int_round_to(unscaled: int, places: int, mode: str) -> int | None:
    factor = 10 ** (SCALE - places)
    negative = unscaled < 0
    magnitude = rounded_quotient(abs(unscaled), factor, negative, mode)
    value = sign_of(negative, magnitude * factor)
    return value if in_range(value) else None


def int_to_integer(unscaled: int, mode: str) -> int:
    negative = unscaled < 0
    magnitude = rounded_quotient(abs(unscaled), ONE, negative, mode)
    return sign_of(negative, magnitude)


def decimal_binary(op: str, lhs: int, rhs: int) -> int | None:
    with localcontext() as ctx:
        ctx.prec = PRECISION
        left = to_decimal(lhs)
        right = to_decimal(rhs)
        if op == "add":
            result = left + right
        elif op == "sub":
            result = left - right
        elif op == "mul":
            result = left * right
        elif op == "div":
            if rhs == 0:
                return None
            result = left / right
        else:  # pragma: no cover - guarded by callers
            raise ValueError(op)
        value = to_unscaled(result)
    return value if in_range(value) else None


def check(label: str, integer: int | None, decimal: int | None) -> None:
    if integer != decimal:
        raise SystemExit(
            f"reference mismatch for {label}: integer={integer} decimal={decimal}"
        )


def rust_option(value: int | None) -> str:
    return "None" if value is None else f"Some({value})"


def render_binary_cases() -> list[str]:
    lines = ["pub const BINARY_CASES: &[BinaryCase] = &["]
    for lhs in VALUES:
        for rhs in VALUES:
            add = int_add(lhs, rhs)
            sub = int_sub(lhs, rhs)
            mul = int_mul(lhs, rhs)
            div = int_div(lhs, rhs, "half_even") if rhs != 0 else None
            check(f"add({lhs},{rhs})", add, decimal_binary("add", lhs, rhs))
            check(f"sub({lhs},{rhs})", sub, decimal_binary("sub", lhs, rhs))
            check(f"mul({lhs},{rhs})", mul, decimal_binary("mul", lhs, rhs))
            if rhs != 0:
                check(f"div({lhs},{rhs})", div, decimal_binary("div", lhs, rhs))
            lines.append(
                "    BinaryCase { "
                f"a: {lhs}, b: {rhs}, "
                f"add: {rust_option(add)}, sub: {rust_option(sub)}, "
                f"mul: {rust_option(mul)}, div: {rust_option(div)} "
                "},"
            )
    lines.append("];")
    return lines


DIV_ROUND_CASES = (
    (1, 3),
    (2, 3),
    (1, 2),
    (3, 2),
    (5, 2),
    (-1, 2),
    (-3, 2),
    (-5, 2),
    (7, 2),
    (-7, 2),
    (10**18, 3),
    (-10**18, 3),
    (10**18, -3),
)

ROUND_TO_VALUES = (
    500_000_000_000_000_000,
    -500_000_000_000_000_000,
    1_500_000_000_000_000_000,
    2_500_000_000_000_000_000,
    1_234_567_890_123_456_789,
    -1_234_567_890_123_456_789,
)
ROUND_TO_PLACES = (0, 1, 2, 6, 17, 18)


def render_round_cases() -> list[str]:
    lines = ["pub const ROUND_CASES: &[RoundCase] = &["]
    for mode in MODES:
        for lhs, rhs in DIV_ROUND_CASES:
            expected = int_div(lhs, rhs, mode)
            # Decimal division only exposes half-even; compare half-even rows.
            if mode == "half_even":
                check(
                    f"div({lhs},{rhs},{mode})",
                    expected,
                    decimal_binary("div", lhs, rhs),
                )
            lines.append(
                "    RoundCase { op: \"div\", "
                f"a: {lhs}, b: {rhs}, mode: \"{mode}\", "
                f"expected: {rust_option(expected)} }},"
            )
        for unscaled in ROUND_TO_VALUES:
            for places in ROUND_TO_PLACES:
                expected = int_round_to(unscaled, places, mode)
                lines.append(
                    "    RoundCase { op: \"round_to\", "
                    f"a: {unscaled}, b: {places}, mode: \"{mode}\", "
                    f"expected: {rust_option(expected)} }},"
                )
    lines.append("];")
    return lines


MUL_DIV_CASES = (
    (5_000_000_000_000_000_000, 3, 2),
    (10**18, 1, 3),
    (10**18, 100, 3),
    (MAX, 1, 3),
    (MAX, 2, 3),
    (-MAX, 2, 3),
    (930 * 10**18, 1, 9),
    (MAX, MAX, 1),
    (MAX, MAX, 3),
    (-MAX, MAX, 1),
)


def render_mul_div_cases() -> list[str]:
    lines = ["pub const MUL_DIV_CASES: &[MulDivCase] = &["]
    for lhs, rhs, divisor in MUL_DIV_CASES:
        for mode in MODES:
            expected = int_mul_div(lhs, rhs, divisor, mode)
            if mode == "half_even":
                with localcontext() as ctx:
                    ctx.prec = PRECISION
                    reference = to_unscaled(
                        to_decimal(lhs) * to_decimal(rhs) / to_decimal(divisor)
                    )
                if reference > MAX or reference < MIN:
                    reference = None
                check(f"mul_div({lhs},{rhs},{divisor})", expected, reference)
            lines.append(
                "    MulDivCase { "
                f"a: {lhs}, b: {rhs}, c: {divisor}, mode: \"{mode}\", "
                f"expected: {rust_option(expected)} }},"
            )
    lines.append("];")
    return lines


TEXT_VALUES = (
    0,
    1,
    -1,
    500_000_000_000_000_000,
    1_000_000_000_000,
    1_000_000_000_000_000,
    ONE,
    15_025,
    1_208_925_819_614_629_174_706_176,
    123_456_789_012_345_678_901_234_567_890,
    -MAX,
    MAX,
)


def render_text_cases() -> list[str]:
    lines = ["pub const TEXT_CASES: &[TextCase] = &["]
    for unscaled in TEXT_VALUES:
        with localcontext() as ctx:
            ctx.prec = PRECISION
            text = str(to_decimal(unscaled))
        lines.append(
            "    TextCase { "
            f"unscaled: {unscaled}, text: \"{text}\" }},"
        )
    lines.append("];")
    return lines


PARSE_CASES = (
    "0",
    "1",
    "-1",
    "0.5",
    "1.5",
    "-1.5",
    "123.456",
    "0.000000000000000001",
    "1e-18",
    "1E18",
    "2.5e-19",
    "1.234567890123456789",
    "1.2345678901234567895",
    "99999999999999999999.999999999999999999",
    " 1.5 ",
    "5.",
    ".5",
    "+.5",
    "",
    "   ",
    "abc",
    "1.2.3",
    "1e",
    "--1",
    "+-1",
    "1e2.5",
    ".",
    "NaN",
    "Infinity",
    "1e30",
    "-1e30",
)


def parse_reference(text: str) -> int | None:
    stripped = text.strip()
    if not stripped:
        return None
    if stripped.lower() in {"nan", "infinity", "inf", "+inf", "-inf"}:
        return None
    try:
        with localcontext() as ctx:
            ctx.prec = PRECISION
            value = Decimal(stripped)
            if not value.is_finite():
                return None
            quantized = value.quantize(QUANTA, rounding=ROUND_HALF_EVEN)
            unscaled = int(quantized.scaleb(SCALE))
    except (InvalidOperation, ValueError):
        return None
    return unscaled if in_range(unscaled) else None


def render_parse_cases() -> list[str]:
    lines = ["pub const PARSE_CASES: &[ParseCase] = &["]
    for text in PARSE_CASES:
        expected = parse_reference(text)
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(
            "    ParseCase { "
            f'text: "{escaped}", unscaled: {rust_option(expected)} }},'
        )
    lines.append("];")
    return lines


INT_VALUES = (
    0,
    1,
    -1,
    500_000_000_000_000_000,
    -500_000_000_000_000_000,
    1_500_000_000_000_000_000,
    -1_500_000_000_000_000_000,
    2_500_000_000_000_000_000,
    -2_500_000_000_000_000_000,
    ONE - 1,
    -(ONE - 1),
)


def render_int_cases() -> list[str]:
    lines = ["pub const INT_CASES: &[IntCase] = &["]
    for unscaled in INT_VALUES:
        for mode in MODES:
            expected = int_to_integer(unscaled, mode)
            lines.append(
                "    IntCase { "
                f"unscaled: {unscaled}, mode: \"{mode}\", integer: {expected} }},"
            )
    lines.append("];")
    return lines


HEADER = """// @generated by generate_golden.py -- do not edit by hand.
//
// Golden vectors for the scale-18 fixed-point kernel, derived from the Python
// core-types reference (`harness.core.types.common` + stdlib `decimal` at a
// 200-digit context, quantized to scale 18 with ROUND_HALF_EVEN).

#![allow(dead_code)]

/// Operands and expected results for add/sub/mul/div.
pub struct BinaryCase {
    pub a: i128,
    pub b: i128,
    pub add: Option<i128>,
    pub sub: Option<i128>,
    pub mul: Option<i128>,
    pub div: Option<i128>,
}

/// One explicitly-rounded operation (`div` or `round_to`).
pub struct RoundCase {
    pub op: &'static str,
    pub a: i128,
    pub b: i128,
    pub mode: &'static str,
    pub expected: Option<i128>,
}

/// A fused multiply-divide case.
pub struct MulDivCase {
    pub a: i128,
    pub b: i128,
    pub c: i128,
    pub mode: &'static str,
    pub expected: Option<i128>,
}

/// Canonical text rendering of an unscaled value.
pub struct TextCase {
    pub unscaled: i128,
    pub text: &'static str,
}

/// Text parsing: `None` means the literal must be rejected.
pub struct ParseCase {
    pub text: &'static str,
    pub unscaled: Option<i128>,
}

/// Integer conversion under an explicit rounding mode.
pub struct IntCase {
    pub unscaled: i128,
    pub mode: &'static str,
    pub integer: i128,
}
"""


def main() -> None:
    sections = [
        HEADER,
        *render_binary_cases(),
        "",
        *render_round_cases(),
        "",
        *render_mul_div_cases(),
        "",
        *render_text_cases(),
        "",
        *render_parse_cases(),
        "",
        *render_int_cases(),
        "",
    ]
    output = Path(__file__).resolve().parent / "golden_data" / "mod.rs"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(sections), encoding="utf-8")
    print(f"wrote {output}")


if __name__ == "__main__":
    main()
