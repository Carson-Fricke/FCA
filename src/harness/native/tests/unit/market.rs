use super::{MarketEvent, MarketOrder, OrderType, QuoteEvent, Side, TradeEvent};

const STAMP_NS: i128 = 1_705_674_600_000_000_000;

#[test]
fn market_events_validate_like_the_python_records() {
    let quote = QuoteEvent::new(QuoteEvent {
        event_id: "q",
        timestamp_ns: STAMP_NS,
        sequence: 0,
        symbol: "AAPL",
        bid_price: dec(100),
        bid_quantity: dec(10),
        ask_price: dec(101),
        ask_quantity: dec(10),
        venue: None,
    })
    .unwrap();
    let quote_view = MarketEvent::Quote(quote);
    assert_eq!(quote_view.class_priority(), 0);
    assert_eq!(quote_view.symbol(), "AAPL");

    let trade = TradeEvent::new(TradeEvent {
        event_id: "t",
        timestamp_ns: STAMP_NS,
        sequence: 1,
        symbol: "AAPL",
        price: dec(100) + 500_000_000_000_000_000,
        quantity: 1_000_000_000_000_000_000,
        venue: None,
        trade_id: Some("t-1"),
    })
    .unwrap();
    let trade_view = MarketEvent::Trade(trade);
    assert_eq!(trade_view.class_priority(), 1);

    assert!(QuoteEvent::new(QuoteEvent {
        event_id: "q",
        timestamp_ns: STAMP_NS,
        sequence: 0,
        symbol: "AAPL",
        bid_price: 0,
        bid_quantity: dec(1),
        ask_price: dec(1),
        ask_quantity: dec(1),
        venue: None,
    })
    .is_err());
    assert!(TradeEvent::new(TradeEvent {
        event_id: "t",
        timestamp_ns: STAMP_NS,
        sequence: 0,
        symbol: "aapl",
        price: dec(1),
        quantity: dec(1),
        venue: None,
        trade_id: None,
    })
    .is_err());
    assert!(TradeEvent::new(TradeEvent {
        event_id: "",
        timestamp_ns: STAMP_NS,
        sequence: 0,
        symbol: "AAPL",
        price: dec(1),
        quantity: dec(1),
        venue: None,
        trade_id: None,
    })
    .is_err());
}

#[test]
fn orders_carry_exactly_the_prices_their_type_allows() {
    let market_order = MarketOrder::new(MarketOrder {
        ..MarketOrder::market("m", "AAPL", Side::Buy, dec(10))
    })
    .unwrap();
    assert_eq!(market_order.order_type, OrderType::Market);

    let limit_order = MarketOrder::new(MarketOrder {
        order_id: "l",
        symbol: "AAPL",
        side: Side::Buy,
        quantity: dec(10),
        order_type: OrderType::Limit,
        time_in_force: super::TimeInForce::Day,
        limit_price: Some(dec(101)),
        stop_price: None,
        timestamp_ns: 0,
        sequence: 0,
    })
    .unwrap();
    assert!(limit_order.limit_price.is_some());

    assert!(MarketOrder::new(MarketOrder {
        order_type: OrderType::Limit,
        stop_price: Some(dec(101)),
        ..MarketOrder::market("bad-limit", "AAPL", Side::Buy, dec(10))
    })
    .is_err());
    assert!(MarketOrder::new(MarketOrder {
        limit_price: Some(dec(101)),
        ..MarketOrder::market("bad-market", "AAPL", Side::Buy, dec(10))
    })
    .is_err());
    assert!(MarketOrder::new(MarketOrder {
        order_type: OrderType::StopLimit,
        limit_price: Some(dec(101)),
        ..MarketOrder::market("bad-stop-limit", "AAPL", Side::Buy, dec(10))
    })
    .is_err());
    assert!(MarketOrder::new(MarketOrder {
        quantity: 0,
        ..MarketOrder::market("zero", "AAPL", Side::Buy, 0)
    })
    .is_err());
}

fn dec(value: i64) -> i128 {
    crate::decimal::from_int(value)
}
