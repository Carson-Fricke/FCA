use super::{LatencyMetadata, LatencySize, LatencySizeConfig, RemainderPolicy};
use crate::market::{OrderType, Side};
use crate::simple_touch::testing::{
    dec, expect_decision, large_quote, make_order, make_trade, HUNDRED, ONE0ONE, THREE_HUNDRED,
};
use crate::simple_touch::{DecideOptions, Liquidity};

#[test]
fn displayed_size_bounds_fills_and_applies_remainder_policy() {
    let mut cancel_model = LatencySize::new(LatencySizeConfig {
        remainder_policy: RemainderPolicy::Cancel,
        ..LatencySizeConfig::default()
    });
    let mut rest_model = LatencySize::new(LatencySizeConfig::default());
    let buy = make_order(
        "size-cancel",
        Side::Buy,
        OrderType::Market,
        THREE_HUNDRED,
        None,
        None,
    );

    let first = cancel_model
        .decide(
            &buy,
            &large_quote("q-1", HUNDRED, ONE0ONE),
            &DecideOptions::default(),
        )
        .unwrap();
    expect_decision(first, HUNDRED, ONE0ONE, Liquidity::Taker);
    assert!(cancel_model.is_cancelled(buy.order_id));

    let cancelled_second = cancel_model
        .decide(
            &buy,
            &large_quote("q-2", HUNDRED, ONE0ONE),
            &DecideOptions {
                remaining_quantity: Some(dec(200)),
                ..DecideOptions::default()
            },
        )
        .unwrap();
    assert!(cancelled_second.is_none());

    let rest_first = rest_model
        .decide(
            &buy,
            &large_quote("q-1", HUNDRED, ONE0ONE),
            &DecideOptions::default(),
        )
        .unwrap();
    expect_decision(rest_first, HUNDRED, ONE0ONE, Liquidity::Taker);
    assert!(!rest_model.is_cancelled(buy.order_id));
    let rest_second = rest_model
        .decide(
            &buy,
            &large_quote("q-2", HUNDRED, ONE0ONE),
            &DecideOptions {
                remaining_quantity: Some(dec(200)),
                ..DecideOptions::default()
            },
        )
        .unwrap();
    expect_decision(rest_second, HUNDRED, ONE0ONE, Liquidity::Taker);
}

#[test]
fn partial_fills_across_quote_events_are_snapshot_deterministic() {
    let mut model = LatencySize::new(LatencySizeConfig::default());
    let buy = make_order(
        "snapshots",
        Side::Buy,
        OrderType::Market,
        THREE_HUNDRED,
        None,
        None,
    );
    let first_quote = large_quote("q-1", HUNDRED, ONE0ONE);
    let second_quote = large_quote("q-2", HUNDRED, HUNDRED);

    let first = model
        .decide(&buy, &first_quote, &DecideOptions::default())
        .unwrap();
    let second = model
        .decide(
            &buy,
            &second_quote,
            &DecideOptions {
                remaining_quantity: Some(dec(200)),
                ..DecideOptions::default()
            },
        )
        .unwrap();
    expect_decision(first, HUNDRED, ONE0ONE, Liquidity::Taker);
    expect_decision(second, HUNDRED, HUNDRED, Liquidity::Taker);
    assert_eq!(first.unwrap().triggering_market.event_id(), "q-1");
    assert_eq!(second.unwrap().triggering_market.event_id(), "q-2");

    let repeat = model
        .decide(
            &buy,
            &first_quote,
            &DecideOptions {
                remaining_quantity: Some(HUNDRED),
                ..DecideOptions::default()
            },
        )
        .unwrap();
    assert!(repeat.is_some());
    assert_eq!(first.unwrap().quantity, repeat.unwrap().quantity);
    assert_eq!(first.unwrap().price, repeat.unwrap().price);
}

#[test]
fn inside_spread_limit_requires_a_qualifying_print_by_default() {
    let mut model = LatencySize::new(LatencySizeConfig::default());
    let buy = make_order(
        "inside",
        Side::Buy,
        OrderType::Limit,
        THREE_HUNDRED,
        Some(HUNDRED + 500_000_000_000_000_000),
        None,
    );

    assert!(model
        .decide(
            &buy,
            &large_quote("q-1", HUNDRED, ONE0ONE),
            &DecideOptions::default()
        )
        .unwrap()
        .is_none());
    assert!(model
        .decide(
            &buy,
            &make_trade("t-1", HUNDRED + 750_000_000_000_000_000, HUNDRED),
            &DecideOptions::default()
        )
        .unwrap()
        .is_none());

    let decision = model
        .decide(
            &buy,
            &make_trade("t-2", HUNDRED + 500_000_000_000_000_000, HUNDRED),
            &DecideOptions::default(),
        )
        .unwrap();
    expect_decision(
        decision,
        HUNDRED,
        HUNDRED + 500_000_000_000_000_000,
        Liquidity::Maker,
    );
}

#[test]
fn configuration_changes_latency_and_inside_spread_outcome() {
    let mut pessimistic = LatencySize::new(LatencySizeConfig {
        response_latency_us: 25,
        require_trade_through: true,
        ..LatencySizeConfig::default()
    });
    let mut optimistic = LatencySize::new(LatencySizeConfig {
        response_latency_us: 50,
        require_trade_through: false,
        ..LatencySizeConfig::default()
    });
    let buy = make_order(
        "parameters",
        Side::Buy,
        OrderType::Limit,
        THREE_HUNDRED,
        Some(HUNDRED + 500_000_000_000_000_000),
        None,
    );
    let inside = large_quote("q-1", HUNDRED, ONE0ONE);

    assert_eq!(pessimistic.metadata().response_latency_us, 25);
    assert!(pessimistic
        .decide(&buy, &inside, &DecideOptions::default())
        .unwrap()
        .is_none());
    let decision = optimistic
        .decide(&buy, &inside, &DecideOptions::default())
        .unwrap();
    expect_decision(
        decision,
        HUNDRED,
        HUNDRED + 500_000_000_000_000_000,
        Liquidity::Maker,
    );
    assert_eq!(optimistic.metadata().response_latency_us, 50);
    assert_eq!(
        pessimistic.metadata(),
        LatencyMetadata {
            response_latency_us: 25,
            displayed_size_constrained: true,
            remainder_policy: RemainderPolicy::Rest,
            require_trade_through: true,
        }
    );
}
