use num::Decimal18;

use super::{
    aggregate_fees, AssetClass, CryptoMakerTakerFeeModel, EquityRegulatoryFeeModel, FeeContext,
    FeeRounding, FeeRoundingMode, FeeSchedule, LiquidityRole, OptionsPerContractFeeModel,
};
use crate::accounting::Fill;
use crate::orders::Side;
use crate::symbol::Symbol;

fn dec(value: &str) -> Decimal18 {
    value.parse().unwrap()
}

fn fill(side: Side, quantity: &str, price: &str, liquidity: Option<&str>) -> Fill {
    Fill::new(
        "f1",
        "o1",
        Symbol::new("AAPL").unwrap(),
        side,
        dec(quantity),
        dec(price),
        Decimal18::ZERO,
        liquidity.map(str::to_string),
    )
    .unwrap()
}

fn schedule() -> FeeSchedule {
    let rounding = FeeRounding::new(dec("0.01"), FeeRoundingMode::HalfEven).unwrap();
    FeeSchedule::new(
        "default",
        "USD",
        EquityRegulatoryFeeModel::new(
            rounding,
            dec("0.005"),
            dec("1"),
            dec("0.00278"),
            dec("0.000166"),
            dec("8.30"),
        )
        .unwrap(),
        OptionsPerContractFeeModel::new(rounding, dec("0.65"), dec("0.10")).unwrap(),
        CryptoMakerTakerFeeModel::new(rounding, dec("0.10"), dec("0.25")).unwrap(),
    )
    .unwrap()
}

#[test]
fn equity_buy_pays_commission_only() {
    let context = FeeContext::new("run", "strat", AssetClass::Equity).unwrap();
    let assessment = schedule()
        .calculate(&fill(Side::Buy, "100", "50", None), &context)
        .unwrap();
    assert_eq!(assessment.components.len(), 1);
    assert_eq!(assessment.total(), dec("1.00"));
}

#[test]
fn equity_sell_adds_regulatory_fees() {
    let context = FeeContext::new("run", "strat", AssetClass::Equity).unwrap();
    let assessment = schedule()
        .calculate(&fill(Side::Sell, "1000", "50", None), &context)
        .unwrap();
    assert_eq!(assessment.components.len(), 3);
    let ids: Vec<_> = assessment
        .components
        .iter()
        .map(|component| component.fee_id.as_str())
        .collect();
    assert!(ids.contains(&"sec-regulatory"));
    assert!(ids.contains(&"finra-taf"));
}

#[test]
fn crypto_maker_rebate_and_taker_charge() {
    let context = FeeContext::new("run", "strat", AssetClass::Crypto).unwrap();
    let taker = schedule()
        .calculate(&fill(Side::Buy, "200", "100", Some("taker")), &context)
        .unwrap();
    assert_eq!(taker.total(), dec("0.50"));
    let maker = schedule()
        .calculate(&fill(Side::Buy, "200", "100", Some("maker")), &context)
        .unwrap();
    assert_eq!(maker.total(), dec("0.20"));
    assert_eq!(LiquidityRole::parse("maker").unwrap(), LiquidityRole::Maker);
}

#[test]
fn options_require_integral_contracts() {
    let context = FeeContext::new("run", "strat", AssetClass::Option).unwrap();
    assert!(schedule()
        .calculate(&fill(Side::Buy, "1.5", "1", None), &context)
        .is_err());
    assert!(schedule()
        .calculate(&fill(Side::Buy, "2", "1", None), &context)
        .is_ok());
}

#[test]
fn aggregation_totals_by_every_dimension() {
    let context = FeeContext::new("run", "strat", AssetClass::Equity).unwrap();
    let schedule = schedule();
    let first_fill = fill(Side::Buy, "100", "50", None);
    let mut second_fill = fill(Side::Buy, "200", "50", None);
    second_fill.fill_id = "f2".to_string();
    let (_, first) = schedule.apply(&first_fill, &context).unwrap();
    let (_, second) = schedule.apply(&second_fill, &context).unwrap();
    let aggregation = aggregate_fees(&[first, second]).unwrap();
    assert_eq!(aggregation.by_run.len(), 1);
    assert_eq!(aggregation.by_strategy[0].amount, dec("2.00"));
    assert_eq!(aggregation.by_asset_class[0].key, "equity");
}
