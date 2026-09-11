use super::{FillModel, LatencySize, LatencySizeConfig};
use crate::market::{OrderType, Side};
use crate::simple_touch::testing::{default_quote, expect_decision, make_order, ONE0ONE, TEN};
use crate::simple_touch::{DecideOptions, Liquidity};

#[test]
fn fill_model_interface_dispatches_by_plugin_kind() {
    let buy = make_order("interface", Side::Buy, OrderType::Market, TEN, None, None);
    let mut touch = FillModel::SimpleTouch;
    let via_touch = touch
        .decide(&buy, &default_quote("q"), &DecideOptions::default())
        .unwrap();
    expect_decision(via_touch, TEN, ONE0ONE, Liquidity::Taker);
    assert!(touch.is_simple_touch());

    let mut latency = FillModel::LatencySize(LatencySize::new(LatencySizeConfig::default()));
    let via_latency = latency
        .decide(&buy, &default_quote("q"), &DecideOptions::default())
        .unwrap();
    expect_decision(via_latency, TEN, ONE0ONE, Liquidity::Taker);
    assert!(!latency.is_simple_touch());
}
