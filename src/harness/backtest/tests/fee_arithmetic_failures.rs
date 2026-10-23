//! Fee arithmetic that leaves the exact decimal range fails by name.
//!
//! `FeeSchedule::calculate` is the public fee boundary. Each test builds a
//! schedule from its public fields and a fill whose exact component product
//! cannot be represented, and asserts a named error rather than a zero fee. A
//! valid zero quantity still yields zero components.

use backtest::backtest::fees::CryptoMakerTakerFeeModel;
use backtest::backtest::fees::EquityRegulatoryFeeModel;
use backtest::backtest::fees::FeeFill;
use backtest::backtest::fees::FeeRounding;
use backtest::backtest::fees::FeeSchedule;
use backtest::backtest::fees::OptionsPerContractFeeModel;
use harness_trading::replay_errors::BacktestError;
use num::Decimal;
use num::RoundingMode;

fn dec(text: &str) -> Decimal {
    text.parse().expect("decimal literal")
}

/// A schedule whose per-share commission is `commission_per_share`.
///
/// The scales of the literals are chosen so a product of two of them stays
/// representable, except where a test deliberately pairs very small scales.
fn schedule(commission_per_share: &str) -> FeeSchedule {
    let rounding = FeeRounding::new(dec("0.0001"), RoundingMode::HalfEven).unwrap();
    FeeSchedule {
        schedule_id: "test-schedule".to_string(),
        currency: "USD".to_string(),
        equity: EquityRegulatoryFeeModel {
            rounding,
            commission_per_share: dec(commission_per_share),
            commission_minimum: dec("0"),
            sec_rate_bps: dec("0.278"),
            taf_per_share: dec("0.000166"),
            taf_maximum: dec("8.30"),
        },
        option: OptionsPerContractFeeModel {
            rounding,
            commission_per_contract: dec("0.65"),
            fee_per_contract: dec("0.10"),
        },
        crypto: CryptoMakerTakerFeeModel {
            rounding,
            maker_rate_bps: dec("10"),
            taker_rate_bps: dec("25"),
        },
    }
}

fn fill<'a>(
    fill_id: &'a str,
    side: &'a str,
    quantity: Decimal,
    price: Decimal,
    liquidity: Option<&'a str>,
) -> FeeFill<'a> {
    FeeFill {
        fill_id,
        side,
        quantity,
        price,
        liquidity,
    }
}

#[test]
fn commission_product_outside_the_exact_range_fails_instead_of_zeroing_the_fee() {
    // The quantity and rate scales sum past the representable scale range.
    let buy = fill("fill-1", "buy", dec("1E-1500000000"), dec("100"), None);
    let error = schedule("1E-1000000000")
        .calculate(&buy, "job-1", "strategy-a", "equity")
        .expect_err("the commission product cannot be represented exactly");
    assert!(
        matches!(error, BacktestError::Arithmetic(ref message) if message.contains("commission") && message.contains("fill-1"))
    );
}

#[test]
fn crypto_notional_outside_the_exact_range_fails_instead_of_zeroing_the_fee() {
    let taker = fill(
        "fill-1",
        "buy",
        dec("1E-1500000000"),
        dec("1E-1000000000"),
        Some("taker"),
    );
    let error = schedule("0.005")
        .calculate(&taker, "job-1", "strategy-a", "crypto")
        .expect_err("the crypto notional cannot be represented exactly");
    assert!(matches!(error, BacktestError::Arithmetic(_)));
}

#[test]
fn sell_side_sec_fee_outside_the_exact_range_fails_instead_of_zeroing_the_fee() {
    // The commission product still fits; the SEC notional product does not.
    let sell = fill(
        "fill-1",
        "sell",
        dec("1E-1500000000"),
        dec("1E-1000000000"),
        None,
    );
    let error = schedule("0.005")
        .calculate(&sell, "job-1", "strategy-a", "equity")
        .expect_err("the SEC notional cannot be represented exactly");
    assert!(
        matches!(error, BacktestError::Arithmetic(ref message) if message.contains("SEC regulatory fee"))
    );
}

#[test]
fn zero_quantity_fill_is_a_valid_zero_fee_not_an_error() {
    let flat = fill("fill-1", "sell", dec("0"), dec("100"), None);
    let assessment = schedule("0.005")
        .calculate(&flat, "job-1", "strategy-a", "equity")
        .expect("a zero quantity is a valid fill");
    assert_eq!(assessment.components().len(), 3);
    for component in assessment.components() {
        assert!(component.amount.cmp_value(Decimal::ZERO).is_eq());
    }
    assert!(assessment.total().cmp_value(Decimal::ZERO).is_eq());
}

#[test]
fn option_fee_overflow_is_reported_instead_of_substituting_the_quantity() {
    let mut fees = schedule("0");
    fees.option.commission_per_contract = dec("1E+1000000000");
    let contracts = fill(
        "option-overflow",
        "buy",
        dec("1E+1500000000"),
        dec("100"),
        None,
    );
    assert!(matches!(
        fees.calculate(&contracts, "job", "strategy", "option"),
        Err(BacktestError::Arithmetic(_))
    ));
}
#[test]
fn fractional_option_contracts_are_rejected_by_the_shared_fee_policy() {
    let contracts = fill("fractional", "buy", dec("1.5"), dec("100"), None);
    assert!(fees_error(&contracts).contains("whole number"));
}
fn fees_error(contracts: &FeeFill<'_>) -> String {
    schedule("0")
        .calculate(contracts, "job", "strategy", "option")
        .unwrap_err()
        .to_string()
}
