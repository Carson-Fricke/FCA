//! Golden and property tests for the Python-compatible variable-scale decimal.
//!
//! The vectors in `pydecimal_data` are generated from CPython's `decimal`
//! module by `generate_pydecimal_golden.py`. Each case asserts the exact
//! `str(Decimal)` rendering, so arithmetic scale, exponent formatting, and
//! rounding behavior are all covered byte for byte.

use core::str::FromStr;

use num::{Context, DecimalError, PyDecimal, RoundingMode, INDICATOR_PRECISION};

mod pydecimal_data;

use pydecimal_data::{
    ArithCase, InvalidCase, ParseCase, QuantizeCase, ARITH_CASES, CANONICAL_TEXT, DIV_MODE_CASES,
    FORMAT_CASES, INDICATOR_CASES, INTEGRAL_CASES, PARSE_INVALID, PARSE_VALID, PROPERTY_CASES,
    QUANTIZE_CASES, QUANTIZE_MODE_CASES, UNARY_CASES,
};

fn dec(text: &str) -> PyDecimal {
    PyDecimal::from_str(text).unwrap_or_else(|error| panic!("failed to parse {text:?}: {error}"))
}

#[test]
fn parse_valid_matches_python() {
    for case in PARSE_VALID {
        let ParseCase {
            text,
            coefficient,
            scale,
            negative,
            display,
        } = *case;
        let value = dec(text);
        assert_eq!(value.coefficient(), coefficient, "coefficient for {text:?}");
        assert_eq!(value.scale(), scale, "scale for {text:?}");
        assert_eq!(value.is_signed(), negative, "sign for {text:?}");
        assert_eq!(value.to_string(), display, "display for {text:?}");
    }
}

#[test]
fn parse_invalid_is_rejected() {
    for case in PARSE_INVALID {
        let InvalidCase { text, nonfinite } = *case;
        match PyDecimal::from_str(text) {
            Err(DecimalError::NonFinite) if nonfinite => {}
            Err(DecimalError::InvalidText) if !nonfinite => {}
            other => panic!("{text:?} should be rejected, got {other:?}"),
        }
    }
}

#[test]
fn display_matches_python() {
    for case in FORMAT_CASES {
        assert_eq!(
            dec(case.text).to_string(),
            case.display,
            "display {:?}",
            case.text
        );
    }
}

fn assert_arith(case: &ArithCase, context: &Context, label: &str) {
    let ArithCase {
        a,
        b,
        add,
        sub,
        mul,
        div,
    } = *case;
    let left = dec(a);
    let right = dec(b);
    assert_eq!(
        left.checked_add_with(right, context).unwrap().to_string(),
        add,
        "{label}: {a} + {b}"
    );
    assert_eq!(
        left.checked_sub_with(right, context).unwrap().to_string(),
        sub,
        "{label}: {a} - {b}"
    );
    assert_eq!(
        left.checked_mul_with(right, context).unwrap().to_string(),
        mul,
        "{label}: {a} * {b}"
    );
    if let Some(expected) = div {
        assert_eq!(
            left.checked_div_with(right, context).unwrap().to_string(),
            expected,
            "{label}: {a} / {b}"
        );
    }
}

#[test]
fn default_context_arithmetic_matches_python() {
    let context = Context::default();
    for case in ARITH_CASES {
        assert_arith(case, &context, "arith");
    }
}

#[test]
fn property_random_python_vectors_match() {
    let context = Context::default();
    for case in PROPERTY_CASES {
        assert_arith(case, &context, "property");
        let display = dec(case.a).to_string();
        assert_eq!(
            dec(&display).to_string(),
            display,
            "display/parse round trip for {:?}",
            case.a
        );
    }
}

#[test]
fn indicator_context_arithmetic_matches_python() {
    let context = Context::new(INDICATOR_PRECISION, RoundingMode::HalfEven);
    for case in INDICATOR_CASES {
        let case = ArithCase {
            a: case.a,
            b: case.b,
            add: case.add,
            sub: case.sub,
            mul: case.mul,
            div: case.div,
        };
        assert_arith(&case, &context, "indicator");
    }
}

#[test]
fn quantize_matches_python() {
    let context = Context::default();
    for case in QUANTIZE_CASES {
        let QuantizeCase {
            value,
            exp,
            expected,
        } = *case;
        let result = dec(value).quantize_with(dec(exp), &context);
        match expected {
            Some(text) => assert_eq!(
                result
                    .unwrap_or_else(|error| panic!("quantize({value}, {exp}): {error}"))
                    .to_string(),
                text,
                "quantize({value}, {exp})"
            ),
            None => assert_eq!(
                result,
                Err(DecimalError::InvalidOperation),
                "quantize({value}, {exp}) should be invalid"
            ),
        }
    }
}

fn rounding(name: &str) -> RoundingMode {
    RoundingMode::from_name(name).unwrap_or_else(|| panic!("unknown rounding mode {name}"))
}

#[test]
fn division_rounding_modes_match_python() {
    for case in DIV_MODE_CASES {
        let context = Context::new(28, rounding(case.mode));
        let result = dec(case.a).checked_div_with(dec(case.b), &context).unwrap();
        assert_eq!(
            result.to_string(),
            case.expected,
            "{} / {} ({})",
            case.a,
            case.b,
            case.mode
        );
    }
}

#[test]
fn quantize_rounding_modes_match_python() {
    for case in QUANTIZE_MODE_CASES {
        let context = Context::new(28, rounding(case.mode));
        let result = dec(case.value).quantize_with(dec(case.exp), &context);
        match case.expected {
            Some(text) => assert_eq!(
                result
                    .unwrap_or_else(|error| panic!(
                        "quantize({}, {}, {}): {error}",
                        case.value, case.exp, case.mode
                    ))
                    .to_string(),
                text,
                "quantize({}, {}, {})",
                case.value,
                case.exp,
                case.mode
            ),
            None => assert_eq!(
                result,
                Err(DecimalError::InvalidOperation),
                "quantize({}, {}, {}) should be invalid",
                case.value,
                case.exp,
                case.mode
            ),
        }
    }
}

#[test]
fn integral_and_normalize_match_python() {
    let context = Context::default();
    for case in INTEGRAL_CASES {
        assert_eq!(
            dec(case.value).to_integral_value(&context).to_string(),
            case.integral,
            "to_integral_value({})",
            case.value
        );
        assert_eq!(
            dec(case.value)
                .normalize_with(&context)
                .unwrap()
                .to_string(),
            case.normalized,
            "normalize({})",
            case.value
        );
    }
}

#[test]
fn unary_matches_python() {
    for case in UNARY_CASES {
        let value = dec(case.value);
        assert_eq!(value.negated().to_string(), case.neg, "-({})", case.value);
        assert_eq!(value.abs().to_string(), case.abs, "abs({})", case.value);
        assert_eq!(value.signum(), case.signum, "signum({})", case.value);
    }
}

#[test]
fn canonical_artifact_values_round_trip() {
    for text in CANONICAL_TEXT {
        assert_eq!(
            dec(text).to_string(),
            *text,
            "canonical round trip {text:?}"
        );
    }
}
