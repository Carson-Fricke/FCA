"""Generate golden vectors for the Python-compatible ``PyDecimal``.

Every vector is produced by the CPython standard library ``decimal`` module at
its default context (precision 28, ``ROUND_HALF_EVEN``), or inside an explicit
``Context(prec=34)`` for the indicator arithmetic context. The output is checked
into the Rust test suite as ``pydecimal_data/mod.rs``; no test evaluates Python
at run time.

The canonical artifact values are read from the backtest golden fixture so the
exact strings that drive byte-identical artifacts are covered.

Run from the repository root::

    uv run python rust/crates/harness-num/tests/generate_pydecimal_golden.py
"""

from __future__ import annotations

import json
import random
from decimal import (
    ROUND_CEILING,
    ROUND_DOWN,
    ROUND_FLOOR,
    ROUND_HALF_DOWN,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    Context,
    Decimal,
    InvalidOperation,
)
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUTPUT = HERE / "pydecimal_data" / "mod.rs"
CANONICAL_FIXTURE = (
    HERE.parents[2]
    / "harness-backtest"
    / "tests"
    / "golden"
    / "backtest_canonical.json"
)

# Values that appear verbatim in the backtest canonical artifact. Kept as a
# fallback when the fixture is not present in a partial checkout.
CANONICAL_FALLBACK = (
    "0",
    "1",
    "20",
    "1.0",
    "0.0000",
    "0.1000",
    "2.6000",
    "-2.8000",
    "450.015",
    "450.025",
    "450.065",
    "450.085",
    "450.125",
    "450.155",
    "450.175",
    "450.235",
    "450.245",
    "450.285",
    "450.295",
    "450.305",
    "450.315",
    "450.335",
    "450.375",
    "450.405",
    "450.415",
    "450.425",
    "450.445",
    "450.465",
    "450.505",
    "450.535",
    "450.550",
    "450.555",
    "100000.00",
    "100000.000",
    "99997.2000",
)

DEFAULT = Context(prec=28, rounding=ROUND_HALF_EVEN)
INDICATOR = Context(prec=34, rounding=ROUND_HALF_EVEN)

FORMAT_TEXTS = (
    "0",
    "-0",
    "0.00",
    "-0.00",
    "0E+2",
    "1",
    "100",
    "1.5",
    "1.500",
    "1.00",
    "10",
    "1E+2",
    "1.2E+3",
    "0.000001",
    "1E-7",
    "0.000",
    "100.00",
    "0.123456789012345678",
    "123.000000000000000000",
    "1E-18",
    "5E-19",
    "99999999999999999999999999999999999999",
    "-99999999999999999999999999999999999999",
)

PARSE_VALID_TEXTS = (
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
    "1e2",
    "-0.00",
    "0.00",
    "-0",
    "00100",
    "1_000.5",
    "1E+2",
    "0E+2",
    "1e0",
    "1e-0",
) + FORMAT_TEXTS

PARSE_INVALID_TEXTS = (
    "",
    "   ",
    "abc",
    "1.2.3",
    "1e",
    "1e+",
    "--1",
    "+-1",
    "1e2.5",
    ".",
    "1e1e1",
    "-",
    "+",
    "1 2",
)

PARSE_NONFINITE_TEXTS = ("NaN", "sNaN", "Infinity", "-Infinity", "inf", "-inf")

FIXED_ARITH = (
    ("100", "1"),
    ("100", "2"),
    ("100", "3"),
    ("1.5", "2"),
    ("1.50", "2"),
    ("1.5", "2.5"),
    ("10", "2"),
    ("10.0", "2"),
    ("1", "4"),
    ("1.0", "4"),
    ("1", "2"),
    ("1", "8"),
    ("1", "3"),
    ("1.00", "3"),
    ("2.50", "0.5"),
    ("100", "0.01"),
    ("1E+2", "1"),
    ("1E+2", "5"),
    ("0", "3"),
    ("0.00", "3"),
    ("1", "7"),
    ("-2.5", "1.00"),
    ("0.10", "0.20"),
    ("9999999999999999999999999999", "1"),
    ("1234567890123456789012345678", "1"),
    ("-1.50", "0.25"),
    ("123.456", "0.001"),
    ("123456789012345678.123456789012345678", "1"),
    ("123456789012345678.123456789012345678", "0.000000000000000001"),
    ("999999999999999999.999999999999999999", "3"),
    ("1", "6"),
    ("1", "9"),
    ("2", "3"),
    ("9999999999999999999999999999", "7"),
    ("10000000000000000000000000000", "3"),
    ("1000000000000000000000000000000", "3"),
    ("1", "9999999999999999999999999999"),
    ("1.234567890123456789012345678901", "3"),
    ("-0", "0"),
    ("0", "-0"),
    ("-0", "-0"),
    ("-0", "0.00"),
    ("-0.0", "-0.00"),
    ("-1", "1"),
    ("1", "-1"),
    ("-0", "-5"),
    ("-0", "5"),
    ("0", "-5"),
)

ROUNDING_MODES = (
    ("half_even", ROUND_HALF_EVEN),
    ("half_up", ROUND_HALF_UP),
    ("half_down", ROUND_HALF_DOWN),
    ("truncate", ROUND_DOWN),
    ("floor", ROUND_FLOOR),
    ("ceiling", ROUND_CEILING),
)

DIV_MODE_OPERANDS = (
    ("1", "3"),
    ("2", "3"),
    ("-1", "3"),
    ("1", "2"),
    ("-1", "2"),
    ("5", "2"),
    ("-5", "2"),
    ("7", "2"),
    ("-7", "2"),
    ("1", "6"),
    ("-1", "6"),
    ("1", "10000000000000000000000000000"),
)

QUANTIZE_MODE_OPERANDS = (
    ("1.5", "1"),
    ("2.5", "1"),
    ("-1.5", "1"),
    ("-2.5", "1"),
    ("1.2345", "0.01"),
    ("-1.2345", "0.01"),
    ("1.2355", "0.01"),
    ("0.1000", "0.01"),
    ("99.95", "0.1"),
    ("-99.95", "0.1"),
)

QUANTIZE_CASES = (
    ("1.2345", "0.01"),
    ("1.2355", "0.01"),
    ("1.2", "0.01"),
    ("1.5", "1"),
    ("2.5", "1"),
    ("-2.5", "1"),
    ("1.5", "0.1"),
    ("123.45", "1E+2"),
    ("123.45", "0.001"),
    ("1.00", "0.1"),
    ("1E+2", "0.01"),
    ("0", "0.00"),
    ("-0", "0.00"),
    ("450.125", "0.0001"),
    ("99997.2000", "0.001"),
    ("1e30", "1"),
    ("9999999999999999999999999999", "1"),
    ("1.5", "1e-30"),
)

INTEGRAL_CASES = (
    "1e30",
    "1e28",
    "123.456",
    "0.5",
    "1.5",
    "2.5",
    "-0.00",
    "1.20",
    "100.00",
    "0.0",
    "1E+2",
    "-2.5",
)

UNARY_CASES = ("0", "-0", "0.00", "1.5", "-1.5", "100", "-450.025")


def _digits_of(value: Decimal) -> tuple[int, int, bool]:
    sign, digits, exponent = value.as_tuple()
    coefficient = 0
    for digit in digits:
        coefficient = coefficient * 10 + digit
    return coefficient, -int(exponent), bool(sign)


def rust_str(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def rust_opt(text: str | None) -> str:
    return "None" if text is None else f"Some({rust_str(text)})"


def canonical_values() -> tuple[str, ...]:
    if not CANONICAL_FIXTURE.exists():
        return CANONICAL_FALLBACK
    with CANONICAL_FIXTURE.open(encoding="utf-8") as handle:
        data = json.load(handle)
    found: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str):
            text = node.strip()
            if not text:
                return
            try:
                Decimal(text)
            except InvalidOperation:
                return
            found.add(text)

    walk(data)
    return (
        tuple(sorted(found, key=lambda item: (len(item), item))) or CANONICAL_FALLBACK
    )


def random_decimal(rng: random.Random) -> str:
    digits = rng.randint(1, 12)
    coefficient = rng.randint(0, 10**digits - 1)
    scale = rng.randint(0, 10)
    body = str(coefficient).rjust(scale + 1, "0")
    if scale:
        body = f"{body[:-scale]}.{body[-scale:]}"
    sign = "-" if rng.random() < 0.35 else ""
    return f"{sign}{body}"


def binary_result(op: str, left: Decimal, right: Decimal) -> str:
    if op == "add":
        return str(left + right)
    if op == "sub":
        return str(left - right)
    if op == "mul":
        return str(left * right)
    if op == "div":
        return str(left / right)
    raise ValueError(op)


def render_parse() -> list[str]:
    lines = ["pub const PARSE_VALID: &[ParseCase] = &["]
    for text in PARSE_VALID_TEXTS:
        value = Decimal(text.strip())
        coefficient, scale, negative = _digits_of(value)
        lines.append(
            "    ParseCase { "
            f"text: {rust_str(text)}, coefficient: {coefficient}, scale: {scale}, "
            f"negative: {str(negative).lower()}, display: {rust_str(str(value))} }},"
        )
    lines.append("];")
    return lines


def render_parse_invalid() -> list[str]:
    lines = ["pub const PARSE_INVALID: &[InvalidCase] = &["]
    for text in PARSE_INVALID_TEXTS:
        lines.append(f"    InvalidCase {{ text: {rust_str(text)}, nonfinite: false }},")
    for text in PARSE_NONFINITE_TEXTS:
        lines.append(f"    InvalidCase {{ text: {rust_str(text)}, nonfinite: true }},")
    lines.append("];")
    return lines


def render_format() -> list[str]:
    lines = ["pub const FORMAT_CASES: &[FormatCase] = &["]
    for text in tuple(dict.fromkeys(FORMAT_TEXTS + PARSE_VALID_TEXTS)):
        value = Decimal(text.strip())
        lines.append(
            f"    FormatCase {{ text: {rust_str(text)}, display: {rust_str(str(value))} }},"
        )
    lines.append("];")
    return lines


def render_arith() -> list[str]:
    lines = ["pub const ARITH_CASES: &[ArithCase] = &["]
    pairs: list[tuple[str, str]] = list(FIXED_ARITH)
    canonical = canonical_values()
    for index, left in enumerate(canonical):
        right = canonical[(index * 7 + 3) % len(canonical)]
        pairs.append((left, right))
    for index, left in enumerate(canonical):
        if index % 4 == 0:
            pairs.append((left, canonical[(index * 5 + 1) % len(canonical)]))
    seen: set[tuple[str, str]] = set()
    for left_text, right_text in pairs:
        if (left_text, right_text) in seen:
            continue
        seen.add((left_text, right_text))
        left = Decimal(left_text)
        right = Decimal(right_text)
        add = binary_result("add", left, right)
        sub = binary_result("sub", left, right)
        mul = binary_result("mul", left, right)
        div = None if right == 0 else binary_result("div", left, right)
        lines.append(
            "    ArithCase { "
            f"a: {rust_str(left_text)}, b: {rust_str(right_text)}, "
            f"add: {rust_str(add)}, sub: {rust_str(sub)}, mul: {rust_str(mul)}, "
            f"div: {rust_opt(div)} }},"
        )
    lines.append("];")
    return lines


def render_quantize() -> list[str]:
    lines = ["pub const QUANTIZE_CASES: &[QuantizeCase] = &["]
    for value_text, exp_text in QUANTIZE_CASES:
        value = Decimal(value_text)
        try:
            expected = str(value.quantize(Decimal(exp_text)))
        except InvalidOperation:
            expected = None
        lines.append(
            "    QuantizeCase { "
            f"value: {rust_str(value_text)}, exp: {rust_str(exp_text)}, "
            f"expected: {rust_opt(expected)} }},"
        )
    lines.append("];")
    return lines


def render_integral() -> list[str]:
    lines = ["pub const INTEGRAL_CASES: &[IntegralCase] = &["]
    for text in INTEGRAL_CASES:
        value = Decimal(text)
        lines.append(
            "    IntegralCase { "
            f"value: {rust_str(text)}, integral: {rust_str(str(value.to_integral_value()))}, "
            f"normalized: {rust_str(str(value.normalize()))} }},"
        )
    lines.append("];")
    return lines


def render_unary() -> list[str]:
    lines = ["pub const UNARY_CASES: &[UnaryCase] = &["]
    for text in UNARY_CASES:
        value = Decimal(text)
        signum = -1 if value < 0 else (1 if value > 0 else 0)
        lines.append(
            "    UnaryCase { "
            f"value: {rust_str(text)}, neg: {rust_str(str(-value))}, "
            f"abs: {rust_str(str(abs(value)))}, signum: {signum} }},"
        )
    lines.append("];")
    return lines


def render_indicator() -> list[str]:
    rng = random.Random(0x1D1C_A70E)
    lines = ["pub const INDICATOR_CASES: &[IndicatorCase] = &["]
    for _ in range(48):
        left_text = random_decimal(rng)
        right_text = random_decimal(rng)
        left = Decimal(left_text)
        right = Decimal(right_text)
        add = str(INDICATOR.add(left, right))
        sub = str(INDICATOR.subtract(left, right))
        mul = str(INDICATOR.multiply(left, right))
        div = "None" if right == 0 else rust_opt(str(INDICATOR.divide(left, right)))
        lines.append(
            "    IndicatorCase { "
            f"a: {rust_str(left_text)}, b: {rust_str(right_text)}, "
            f"add: {rust_str(add)}, sub: {rust_str(sub)}, mul: {rust_str(mul)}, "
            f"div: {div} }},"
        )
    lines.append("];")
    return lines


def render_div_modes() -> list[str]:
    lines = ["pub const DIV_MODE_CASES: &[RoundedCase] = &["]
    for mode_name, rounding in ROUNDING_MODES:
        context = Context(prec=28, rounding=rounding)
        for left_text, right_text in DIV_MODE_OPERANDS:
            result = context.divide(Decimal(left_text), Decimal(right_text))
            lines.append(
                "    RoundedCase { "
                f"a: {rust_str(left_text)}, b: {rust_str(right_text)}, "
                f"mode: {rust_str(mode_name)}, expected: {rust_str(str(result))} }},"
            )
    lines.append("];")
    return lines


def render_quantize_modes() -> list[str]:
    lines = ["pub const QUANTIZE_MODE_CASES: &[QuantizeModeCase] = &["]
    for mode_name, rounding in ROUNDING_MODES:
        context = Context(prec=28, rounding=rounding)
        for value_text, exp_text in QUANTIZE_MODE_OPERANDS:
            try:
                expected = str(context.quantize(Decimal(value_text), Decimal(exp_text)))
            except InvalidOperation:
                expected = None
            lines.append(
                "    QuantizeModeCase { "
                f"value: {rust_str(value_text)}, exp: {rust_str(exp_text)}, "
                f"mode: {rust_str(mode_name)}, expected: {rust_opt(expected)} }},"
            )
    lines.append("];")
    return lines


def render_property() -> list[str]:
    rng = random.Random(0xC0FFEE)
    lines = ["pub const PROPERTY_CASES: &[ArithCase] = &["]
    for _ in range(320):
        left_text = random_decimal(rng)
        right_text = random_decimal(rng)
        left = Decimal(left_text)
        right = Decimal(right_text)
        add = binary_result("add", left, right)
        sub = binary_result("sub", left, right)
        mul = binary_result("mul", left, right)
        div = None if right == 0 else binary_result("div", left, right)
        lines.append(
            "    ArithCase { "
            f"a: {rust_str(left_text)}, b: {rust_str(right_text)}, "
            f"add: {rust_str(add)}, sub: {rust_str(sub)}, mul: {rust_str(mul)}, "
            f"div: {rust_opt(div)} }},"
        )
    lines.append("];")
    return lines


def render_canonical() -> list[str]:
    values = canonical_values()
    lines = ["pub const CANONICAL_TEXT: &[&str] = &["]
    for text in values:
        lines.append(f"    {rust_str(text)},")
    lines.append("];")
    return lines


HEADER = """// @generated by generate_pydecimal_golden.py -- do not edit by hand.
//
// Golden vectors for the Python-compatible decimal (`decimal.Decimal` at the
// default context, precision 28, ROUND_HALF_EVEN; plus explicit prec=34
// indicator vectors). Canonical artifact strings are read from the backtest
// golden fixture.

#![allow(dead_code)]

/// A parseable literal and its expected internal representation.
pub struct ParseCase {
    pub text: &'static str,
    pub coefficient: u128,
    pub scale: i32,
    pub negative: bool,
    pub display: &'static str,
}

/// A literal that must be rejected; `nonfinite` selects NaN/Infinity.
pub struct InvalidCase {
    pub text: &'static str,
    pub nonfinite: bool,
}

/// A literal whose canonical display is asserted.
pub struct FormatCase {
    pub text: &'static str,
    pub display: &'static str,
}

/// Operands and their default-context add/sub/mul/div results.
pub struct ArithCase {
    pub a: &'static str,
    pub b: &'static str,
    pub add: &'static str,
    pub sub: &'static str,
    pub mul: &'static str,
    pub div: Option<&'static str>,
}

/// A quantize case; `None` means Python raised InvalidOperation.
pub struct QuantizeCase {
    pub value: &'static str,
    pub exp: &'static str,
    pub expected: Option<&'static str>,
}

/// `to_integral_value` and `normalize` results.
pub struct IntegralCase {
    pub value: &'static str,
    pub integral: &'static str,
    pub normalized: &'static str,
}

/// Unary minus, `abs`, and numeric sign.
pub struct UnaryCase {
    pub value: &'static str,
    pub neg: &'static str,
    pub abs: &'static str,
    pub signum: i32,
}

/// Operands evaluated inside `Context(prec=34)`.
pub struct IndicatorCase {
    pub a: &'static str,
    pub b: &'static str,
    pub add: &'static str,
    pub sub: &'static str,
    pub mul: &'static str,
    pub div: Option<&'static str>,
}

/// Division under an explicit rounding mode at precision 28.
pub struct RoundedCase {
    pub a: &'static str,
    pub b: &'static str,
    pub mode: &'static str,
    pub expected: &'static str,
}

/// Quantize under an explicit rounding mode at precision 28.
pub struct QuantizeModeCase {
    pub value: &'static str,
    pub exp: &'static str,
    pub mode: &'static str,
    pub expected: Option<&'static str>,
}
"""


def main() -> None:
    sections = [
        HEADER,
        *render_parse(),
        "",
        *render_parse_invalid(),
        "",
        *render_format(),
        "",
        *render_arith(),
        "",
        *render_quantize(),
        "",
        *render_integral(),
        "",
        *render_unary(),
        "",
        *render_indicator(),
        "",
        *render_div_modes(),
        "",
        *render_quantize_modes(),
        "",
        *render_property(),
        "",
        *render_canonical(),
        "",
    ]
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text("\n".join(sections), encoding="utf-8")
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()
