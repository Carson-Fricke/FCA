//! Equity-curve metric rules at the public metrics boundary.
//!
//! These are numeric edge cases: the closed undefined-value reasons, the
//! per-sample Sharpe (sample standard deviation, not annualized), and the CAGR
//! annualization. The job-level publication of the same rules is pinned in
//! `canonical_trades.rs`.

use backtest::results::metrics::{
    cagr, longest_drawdown, max_drawdown, sharpe_per_sample, total_return, EquityPoint,
    NANOS_PER_YEAR_365,
};
use num::Decimal18;

const DAY: i64 = 86_400_000_000_000;

/// Equity points at a fixed spacing, starting at time zero.
fn curve(values: &[&str], step_ns: i64) -> Vec<EquityPoint> {
    values
        .iter()
        .enumerate()
        .map(|(index, value)| EquityPoint {
            timestamp_ns: index as i64 * step_ns,
            equity: value.parse().expect("decimal"),
        })
        .collect()
}

#[test]
fn per_sample_sharpe_is_mean_over_sample_standard_deviation_without_annualization() {
    // Returns are 0.01, -2/101, and 1/33. The mean over the sample (n - 1)
    // standard deviation is 0.27115502791923196; annualizing by sqrt(252) or
    // using the population deviation would give different values.
    let value = sharpe_per_sample(&curve(&["100", "101", "99", "102"], DAY))
        .expect("arithmetic")
        .expect("defined");
    assert!((value - 0.271_155_027_919_232).abs() < 1e-12, "{value}");
}

#[test]
fn per_sample_sharpe_reports_its_reason_in_spec_order() {
    assert_eq!(
        sharpe_per_sample(&curve(&["100", "101", "102"], DAY)).expect("arithmetic"),
        Err("fewer_than_three_returns"),
        "two returns are not enough"
    );
    assert_eq!(
        sharpe_per_sample(&curve(&["100", "100", "100", "100"], DAY)).expect("arithmetic"),
        Err("zero_volatility"),
        "equal returns have no deviation"
    );
    assert_eq!(
        sharpe_per_sample(&curve(&["100", "50", "-1", "20"], DAY)).expect("arithmetic"),
        Err("negative_equity")
    );
    assert_eq!(
        sharpe_per_sample(&curve(&["100", "0", "5", "6"], DAY)).expect("arithmetic"),
        Err("negative_equity"),
        "a zero before the last sample has no return denominator"
    );
}

#[test]
fn total_return_reports_its_reason_or_the_ratio_minus_one() {
    assert_eq!(
        total_return(&curve(&["100"], DAY)).expect("arithmetic"),
        Err("fewer_than_two_samples")
    );
    assert_eq!(
        total_return(&curve(&["0", "5"], DAY)).expect("arithmetic"),
        Err("nonpositive_initial_equity")
    );
    assert_eq!(
        total_return(&curve(&["5", "6", "-1"], DAY)).expect("arithmetic"),
        Err("negative_equity")
    );
    let ratio = total_return(&curve(&["100", "50"], DAY))
        .expect("arithmetic")
        .expect("defined");
    assert_eq!(ratio, "-0.5".parse::<Decimal18>().expect("decimal"));
}

#[test]
fn drawdown_is_undefined_under_negative_equity_and_exact_otherwise() {
    assert_eq!(
        max_drawdown(&curve(&["10", "-1"], DAY)).expect("arithmetic"),
        Err("negative_equity")
    );
    assert_eq!(
        longest_drawdown(&curve(&["10", "-1"], DAY)).expect("arithmetic"),
        Err("negative_equity")
    );
    let dip = max_drawdown(&curve(&["10", "5", "8"], DAY))
        .expect("arithmetic")
        .expect("defined");
    assert_eq!(dip, "0.5".parse::<Decimal18>().expect("decimal"));
}

#[test]
fn cagr_annualizes_growth_as_a_fraction_not_a_multiplier() {
    // Doubling over one 365-day year is 100 percent per year, so the fraction
    // is 1.0; growing sixteen-fold over four years is 100 percent per year too.
    let one_year = cagr(&curve(&["100", "200"], NANOS_PER_YEAR_365))
        .expect("arithmetic")
        .expect("defined");
    assert!((one_year - 1.0).abs() < 1e-12, "{one_year}");
    let four_years = cagr(&curve(&["100", "1600"], 4 * NANOS_PER_YEAR_365))
        .expect("arithmetic")
        .expect("defined");
    assert!((four_years - 1.0).abs() < 1e-12, "{four_years}");
}

#[test]
fn cagr_reports_its_reason_and_names_a_zero_span() {
    assert_eq!(
        cagr(&curve(&["100", "120"], 60_000_000_000)).unwrap(),
        Err("arithmetic_overflow")
    );

    assert_eq!(
        cagr(&curve(&["100"], DAY)).expect("arithmetic"),
        Err("fewer_than_two_samples")
    );
    assert_eq!(
        cagr(&curve(&["0", "1"], DAY)).expect("arithmetic"),
        Err("nonpositive_initial_equity")
    );
    assert_eq!(
        cagr(&curve(&["1", "-1"], DAY)).expect("arithmetic"),
        Err("negative_equity")
    );
    assert_eq!(
        cagr(&curve(&["100", "120"], 0)).expect("arithmetic"),
        Err("nonpositive_span"),
        "a zero span has no annualization"
    );
}
