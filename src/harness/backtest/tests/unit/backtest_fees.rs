use super::*;
use crate::backtest::config::{FeeConfig, FeeRule};

fn config_with(rules: Vec<FeeRule>) -> FeeConfig {
    FeeConfig {
        currency: "USD".to_string(),
        rounding: "half_even".to_string(),
        rules,
    }
}

#[test]
fn derives_models_from_bases() {
    let fees = config_with(vec![FeeRule {
        fee_id: "commission".to_string(),
        basis: FeeBasis::PerUnit,
        rate: "0.005".parse().unwrap(),
        minimum: "0.01".parse().unwrap(),
        side: "both".to_string(),
    }]);
    let schedule =
        fee_schedule_from_config(&fees, DEFAULT_FEE_PRECISION, "harness-config").expect("s");
    assert_eq!(schedule.equity.commission_per_share.to_string(), "0.005");
    assert_eq!(schedule.equity.commission_minimum.to_string(), "0");
}

#[test]
fn equity_commission_rounds_to_precision() {
    let fees = config_with(vec![FeeRule {
        fee_id: "commission".to_string(),
        basis: FeeBasis::PerUnit,
        rate: "0.005".parse().unwrap(),
        minimum: "0.01".parse().unwrap(),
        side: "both".to_string(),
    }]);
    let schedule =
        fee_schedule_from_config(&fees, DEFAULT_FEE_PRECISION, "harness-config").expect("s");
    let fill = FeeFill {
        fill_id: "sim-fill-3",
        side: "buy",
        quantity: "20".parse().unwrap(),
        price: "450.025".parse().unwrap(),
        liquidity: Some("taker"),
    };
    let assessment = schedule.calculate(&fill, "run", "strategy", "equity");
    assert_eq!(assessment.total().to_string(), "0.1000");
    assert_eq!(assessment.components.len(), 1);
}

#[test]
fn sell_adds_regulatory_components() {
    let fees = config_with(vec![FeeRule {
        fee_id: "commission".to_string(),
        basis: FeeBasis::PerUnit,
        rate: "0.005".parse().unwrap(),
        minimum: "0.01".parse().unwrap(),
        side: "both".to_string(),
    }]);
    let schedule =
        fee_schedule_from_config(&fees, DEFAULT_FEE_PRECISION, "harness-config").expect("s");
    let fill = FeeFill {
        fill_id: "sim-fill-6",
        side: "sell",
        quantity: "20".parse().unwrap(),
        price: "450.015".parse().unwrap(),
        liquidity: Some("taker"),
    };
    let assessment = schedule.calculate(&fill, "run", "strategy", "equity");
    assert_eq!(assessment.total().to_string(), "0.1000");
    assert_eq!(assessment.components.len(), 3);
    assert_eq!(assessment.components[1].amount.to_string(), "0.0000");
}
