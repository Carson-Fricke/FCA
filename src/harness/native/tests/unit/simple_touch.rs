use super::testing::{
    dec, default_quote, expect_decision, large_quote, make_order, make_quote, make_trade, HUNDRED,
    ONE0ONE, TEN,
};
use super::{DecideOptions, Liquidity, SimpleTouch};
use crate::market::{OrderType, Side};

#[test]
fn market_orders_fill_at_the_correct_side() {
    let plugin = SimpleTouch;
    let buy = plugin
        .decide(
            &make_order("market-buy", Side::Buy, OrderType::Market, TEN, None, None),
            &default_quote("q"),
            &DecideOptions::default(),
        )
        .unwrap();
    expect_decision(buy, TEN, ONE0ONE, Liquidity::Taker);
    let sell = plugin
        .decide(
            &make_order(
                "market-sell",
                Side::Sell,
                OrderType::Market,
                TEN,
                None,
                None,
            ),
            &default_quote("q"),
            &DecideOptions::default(),
        )
        .unwrap();
    expect_decision(sell, TEN, HUNDRED, Liquidity::Taker);
}

#[test]
fn limits_cross_at_the_touch_and_rest_when_not_crossing() {
    let plugin = SimpleTouch;
    let crossing = plugin
        .decide(
            &make_order(
                "cross-buy",
                Side::Buy,
                OrderType::Limit,
                TEN,
                Some(ONE0ONE),
                None,
            ),
            &large_quote("crossing", HUNDRED, ONE0ONE),
            &DecideOptions::default(),
        )
        .unwrap();
    expect_decision(crossing, TEN, ONE0ONE, Liquidity::Taker);
    let resting = plugin
        .decide(
            &make_order(
                "rest-buy",
                Side::Buy,
                OrderType::Limit,
                TEN,
                Some(ONE0ONE),
                None,
            ),
            &large_quote("resting", dec(99), dec(102)),
            &DecideOptions::default(),
        )
        .unwrap();
    assert!(resting.is_none());

    let sell_crossing = plugin
        .decide(
            &make_order(
                "cross-sell",
                Side::Sell,
                OrderType::Limit,
                TEN,
                Some(HUNDRED),
                None,
            ),
            &large_quote("crossing-sell", HUNDRED, ONE0ONE),
            &DecideOptions::default(),
        )
        .unwrap();
    expect_decision(sell_crossing, TEN, HUNDRED, Liquidity::Taker);
    let sell_resting = plugin
        .decide(
            &make_order(
                "rest-sell",
                Side::Sell,
                OrderType::Limit,
                TEN,
                Some(HUNDRED),
                None,
            ),
            &large_quote("resting-sell", dec(99), dec(102)),
            &DecideOptions::default(),
        )
        .unwrap();
    assert!(sell_resting.is_none());
}

#[test]
fn partial_quantity_is_bounded_by_displayed_touch_quantity() {
    let plugin = SimpleTouch;
    let decision = plugin
        .decide(
            &make_order("partial", Side::Buy, OrderType::Market, TEN, None, None),
            &make_quote("small-ask", HUNDRED, ONE0ONE, TEN, dec(3)),
            &DecideOptions::default(),
        )
        .unwrap();
    expect_decision(decision, dec(3), ONE0ONE, Liquidity::Taker);
}

#[test]
fn stop_orders_arm_only_on_the_correct_touch() {
    let plugin = SimpleTouch;
    let buy_stop = make_order(
        "stop-buy",
        Side::Buy,
        OrderType::Stop,
        TEN,
        None,
        Some(ONE0ONE),
    );
    assert!(super::is_triggered(&buy_stop, &default_quote("buy-trigger")).unwrap());
    assert!(!super::is_triggered(
        &buy_stop,
        &make_quote("buy-rest", HUNDRED, HUNDRED, TEN, TEN)
    )
    .unwrap());
    assert!(plugin
        .decide(
            &buy_stop,
            &make_quote("buy-rest", HUNDRED, HUNDRED, TEN, TEN),
            &DecideOptions::default()
        )
        .unwrap()
        .is_none());
    assert!(plugin
        .decide(
            &buy_stop,
            &default_quote("buy-trigger"),
            &DecideOptions::default()
        )
        .unwrap()
        .is_some());

    let sell_stop = make_order(
        "stop-sell",
        Side::Sell,
        OrderType::Stop,
        TEN,
        None,
        Some(HUNDRED),
    );
    assert!(super::is_triggered(
        &sell_stop,
        &make_quote("sell-trigger", HUNDRED, ONE0ONE, TEN, TEN)
    )
    .unwrap());
    assert!(!super::is_triggered(
        &sell_stop,
        &make_quote("sell-rest", ONE0ONE, dec(102), TEN, TEN)
    )
    .unwrap());
    assert!(plugin
        .decide(
            &sell_stop,
            &make_quote("sell-rest", ONE0ONE, dec(102), TEN, TEN),
            &DecideOptions::default()
        )
        .unwrap()
        .is_none());
    assert!(plugin
        .decide(
            &sell_stop,
            &make_quote("sell-trigger", HUNDRED, ONE0ONE, TEN, TEN),
            &DecideOptions::default()
        )
        .unwrap()
        .is_some());
}

#[test]
fn stop_limit_requires_both_trigger_and_limit_touch() {
    let plugin = SimpleTouch;
    let stop_order = make_order(
        "stop-limit",
        Side::Buy,
        OrderType::StopLimit,
        TEN,
        Some(dec(102)),
        Some(ONE0ONE),
    );
    let decision = plugin
        .decide(
            &stop_order,
            &default_quote("trigger"),
            &DecideOptions::default(),
        )
        .unwrap();
    assert!(decision.is_some());
    assert_eq!(decision.unwrap().triggering_market.event_id(), "trigger");

    let no_limit_touch = make_order(
        "stop-limit-rest",
        Side::Buy,
        OrderType::StopLimit,
        TEN,
        Some(HUNDRED + 500_000_000_000_000_000),
        Some(ONE0ONE),
    );
    assert!(plugin
        .decide(
            &no_limit_touch,
            &default_quote("armed"),
            &DecideOptions::default()
        )
        .unwrap()
        .is_none());
}

#[test]
fn trade_snapshots_are_supported_without_float_conversion() {
    let plugin = SimpleTouch;
    let traded = make_trade("trade", HUNDRED + 250_000_000_000_000_000, dec(2));
    let decision = plugin
        .decide(
            &make_order(
                "trade-market",
                Side::Sell,
                OrderType::Market,
                dec(3),
                None,
                None,
            ),
            &traded,
            &DecideOptions::default(),
        )
        .unwrap();
    expect_decision(
        decision,
        dec(2),
        HUNDRED + 250_000_000_000_000_000,
        Liquidity::Taker,
    );
}

#[test]
fn a_trade_does_not_qualify_a_resting_limit_touch() {
    let plugin = SimpleTouch;
    let traded = make_trade("trade-limit", dec(99), TEN);
    let decision = plugin
        .decide(
            &make_order(
                "limit-trade",
                Side::Buy,
                OrderType::Limit,
                dec(2),
                Some(HUNDRED),
                None,
            ),
            &traded,
            &DecideOptions::default(),
        )
        .unwrap();
    assert!(decision.is_none());
}
