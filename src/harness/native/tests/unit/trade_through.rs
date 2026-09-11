use super::{TradeThrough, TradeThroughConfig};
use crate::market::{OrderType, Side};
use crate::simple_touch::testing::{
    dec, expect_decision, large_quote, make_order, make_trade, HUNDRED, THREE_HUNDRED,
};
use crate::simple_touch::{DecideOptions, Liquidity};

const HALF: i128 = 500_000_000_000_000_000;

#[test]
fn strict_model_requires_a_print_beyond_the_limit() {
    let mut model = TradeThrough::new(TradeThroughConfig::default()).unwrap();
    let buy = make_order(
        "strict",
        Side::Buy,
        OrderType::Limit,
        THREE_HUNDRED,
        Some(HUNDRED + HALF),
        None,
    );

    assert!(model
        .decide(
            &buy,
            &large_quote("quote", HUNDRED, HUNDRED + HALF),
            &DecideOptions::default()
        )
        .unwrap()
        .is_none());
    assert!(model
        .decide(
            &buy,
            &make_trade("trade", HUNDRED + HALF, HUNDRED),
            &DecideOptions::default()
        )
        .unwrap()
        .is_none());

    let decision = model
        .decide(
            &buy,
            &make_trade("through", HUNDRED + HALF - 10_000_000_000_000_000, HUNDRED),
            &DecideOptions::default(),
        )
        .unwrap();
    expect_decision(
        decision,
        HUNDRED,
        HUNDRED + HALF - 10_000_000_000_000_000,
        Liquidity::Maker,
    );
}

#[test]
fn print_qualification_is_directional() {
    let mut model = TradeThrough::new(TradeThroughConfig::default()).unwrap();
    let buy = make_order(
        "direction-buy",
        Side::Buy,
        OrderType::Limit,
        THREE_HUNDRED,
        Some(HUNDRED + HALF),
        None,
    );
    assert!(model
        .decide(
            &buy,
            &make_trade("at-limit", HUNDRED + HALF, HUNDRED),
            &DecideOptions::default()
        )
        .unwrap()
        .is_none());
    let buy_through = model
        .decide(
            &buy,
            &make_trade("through", HUNDRED + HALF - 10_000_000_000_000_000, HUNDRED),
            &DecideOptions::default(),
        )
        .unwrap();
    expect_decision(
        buy_through,
        HUNDRED,
        HUNDRED + HALF - 10_000_000_000_000_000,
        Liquidity::Maker,
    );

    let sell = make_order(
        "direction-sell",
        Side::Sell,
        OrderType::Limit,
        THREE_HUNDRED,
        Some(HUNDRED + HALF),
        None,
    );
    assert!(model
        .decide(
            &sell,
            &make_trade("at-limit", HUNDRED + HALF, HUNDRED),
            &DecideOptions::default()
        )
        .unwrap()
        .is_none());
    let sell_through = model
        .decide(
            &sell,
            &make_trade("through", HUNDRED + HALF + 10_000_000_000_000_000, HUNDRED),
            &DecideOptions::default(),
        )
        .unwrap();
    expect_decision(
        sell_through,
        HUNDRED,
        HUNDRED + HALF + 10_000_000_000_000_000,
        Liquidity::Maker,
    );
}

#[test]
fn print_size_and_remainder_policy() {
    let mut cancel_model = TradeThrough::new(TradeThroughConfig {
        remainder_policy: crate::simple_touch::RemainderPolicy::Cancel,
        ..TradeThroughConfig::default()
    })
    .unwrap();
    let mut rest_model = TradeThrough::new(TradeThroughConfig::default()).unwrap();
    let cancel_order = make_order(
        "cancel",
        Side::Buy,
        OrderType::Limit,
        THREE_HUNDRED,
        Some(HUNDRED + HALF),
        None,
    );
    let rest_order = make_order(
        "rest",
        Side::Buy,
        OrderType::Limit,
        THREE_HUNDRED,
        Some(HUNDRED + HALF),
        None,
    );
    let print_event = make_trade("print", HUNDRED + HALF - 10_000_000_000_000_000, HUNDRED);

    let cancel_decision = cancel_model
        .decide(&cancel_order, &print_event, &DecideOptions::default())
        .unwrap();
    let rest_decision = rest_model
        .decide(&rest_order, &print_event, &DecideOptions::default())
        .unwrap();
    assert!(cancel_decision.is_some() && rest_decision.is_some());
    assert_eq!(
        cancel_decision.unwrap().quantity,
        rest_decision.unwrap().quantity
    );
    assert_eq!(rest_decision.unwrap().quantity, HUNDRED);
    assert!(cancel_model.is_cancelled(cancel_order.order_id));
    assert!(!rest_model.is_cancelled(rest_order.order_id));

    let second = rest_model
        .decide(
            &rest_order,
            &make_trade("trade-2", HUNDRED + HALF - 20_000_000_000_000_000, HUNDRED),
            &DecideOptions {
                remaining_quantity: Some(dec(200)),
                ..DecideOptions::default()
            },
        )
        .unwrap();
    assert!(second.is_some());
}

#[test]
fn parameters_change_threshold_and_metadata() {
    let mut default_model = TradeThrough::new(TradeThroughConfig::default()).unwrap();
    let mut configured = TradeThrough::new(TradeThroughConfig {
        response_latency_us: 40,
        minimum_trade_through: 100_000_000_000_000_000,
        strict: false,
        remainder_policy: crate::simple_touch::RemainderPolicy::Rest,
    })
    .unwrap();
    let buy = make_order(
        "parameters",
        Side::Buy,
        OrderType::Limit,
        THREE_HUNDRED,
        Some(HUNDRED + HALF),
        None,
    );

    assert!(default_model
        .decide(
            &buy,
            &make_trade("trade", HUNDRED + HALF, HUNDRED),
            &DecideOptions::default()
        )
        .unwrap()
        .is_none());
    let decision = configured
        .decide(
            &buy,
            &make_trade("configured", HUNDRED + 400_000_000_000_000_000, HUNDRED),
            &DecideOptions::default(),
        )
        .unwrap();
    expect_decision(
        decision,
        HUNDRED,
        HUNDRED + 400_000_000_000_000_000,
        Liquidity::Maker,
    );
    assert_eq!(configured.metadata().response_latency_us, 40);
    assert_eq!(
        configured.metadata().minimum_trade_through,
        100_000_000_000_000_000
    );
}

#[test]
fn rejects_an_invalid_minimum_trade_through() {
    assert!(TradeThrough::new(TradeThroughConfig {
        minimum_trade_through: -10_000_000_000_000_000,
        ..TradeThroughConfig::default()
    })
    .is_err());
}
