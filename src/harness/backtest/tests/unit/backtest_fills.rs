use super::*;

#[test]
fn simple_touch_identity() {
    let plugin = build_fill_model(FillModel::SimpleTouch, None);
    assert_eq!(
        fill_model_identity(&plugin).to_canonical_string(),
        r#"{"model":"simple_touch"}"#
    );
    assert_eq!(fill_model_latency_us(&plugin), 0);
}

#[test]
fn latency_size_identity_carries_options() {
    let options = FillModelOptions {
        response_latency_ns: 5_000,
        remainder_policy: RemainderPolicy::Cancel,
        require_trade_through: false,
        ..FillModelOptions::default()
    };
    let plugin = build_fill_model(FillModel::LatencySize, Some(&options));
    assert_eq!(
        fill_model_identity(&plugin).to_canonical_string(),
        r#"{"model":"latency_size","remainder_policy":"cancel","require_trade_through":false,"response_latency_us":5}"#
    );
    assert_eq!(fill_model_latency_us(&plugin), 5);
}

#[test]
fn trade_through_identity_includes_minimum() {
    let options = FillModelOptions {
        minimum_trade_through: "0.01".to_string(),
        ..FillModelOptions::default()
    };
    let plugin = build_fill_model(FillModel::TradeThrough, Some(&options));
    assert_eq!(
        fill_model_identity(&plugin).to_canonical_string(),
        r#"{"minimum_trade_through":"0.01","model":"trade_through","remainder_policy":"rest","response_latency_us":0,"strict":true}"#
    );
}

#[test]
fn remainder_policy_round_trips() {
    for policy in [RemainderPolicy::Rest, RemainderPolicy::Cancel] {
        assert_eq!(RemainderPolicy::parse(policy.value()), Some(policy));
    }
}
