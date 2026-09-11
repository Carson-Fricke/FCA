use super::{Error, SimulatedVenue};
use crate::decimal::from_int;
use crate::fills::FillModel;
use crate::market::{MarketEvent, MarketOrder, OrderType, QuoteEvent, Side, TimeInForce};
use crate::market_bus::shared_clock;
use crate::oms::{ExecutionType, OrderStatus};

const STAMP_NS: i128 = 1_705_674_600_000_000_000;
const MICRO: i128 = 1_000;

fn hundred() -> i128 {
    from_int(100)
}

fn one0one() -> i128 {
    from_int(101)
}

fn ten() -> i128 {
    from_int(10)
}

fn quote(
    event_id: &'static str,
    offset_us: i128,
    sequence: u64,
    bid: i128,
    ask: i128,
    bid_quantity: i128,
    ask_quantity: i128,
) -> MarketEvent<'static> {
    MarketEvent::Quote(QuoteEvent {
        event_id,
        timestamp_ns: STAMP_NS + offset_us * MICRO,
        sequence,
        symbol: "AAPL",
        bid_price: bid,
        bid_quantity,
        ask_price: ask,
        ask_quantity,
        venue: None,
    })
}

fn default_quote(event_id: &'static str) -> MarketEvent<'static> {
    quote(event_id, 0, 0, hundred(), one0one(), ten(), ten())
}

#[allow(clippy::too_many_arguments)]
fn order(
    order_id: &'static str,
    side: Side,
    order_type: OrderType,
    quantity: i128,
    time_in_force: TimeInForce,
    limit_price: Option<i128>,
    stop_price: Option<i128>,
    offset_us: i128,
) -> MarketOrder<'static> {
    MarketOrder {
        order_id,
        symbol: "AAPL",
        side,
        quantity,
        order_type,
        time_in_force,
        limit_price,
        stop_price,
        timestamp_ns: STAMP_NS + offset_us * MICRO,
        sequence: 0,
    }
}

fn venue(latency_ns: i128) -> SimulatedVenue {
    SimulatedVenue::new(
        FillModel::SimpleTouch,
        latency_ns,
        Some(shared_clock(STAMP_NS)),
    )
    .unwrap()
}

#[test]
fn buy_and_sell_crossing_emit_ack_reports_and_fills() {
    let mut venue = venue(0);
    venue.on_market(default_quote("q")).unwrap();
    assert_eq!(venue.drain().unwrap(), 0);

    venue
        .submit(order(
            "buy",
            Side::Buy,
            OrderType::Limit,
            ten(),
            TimeInForce::Day,
            Some(one0one()),
            None,
            0,
        ))
        .unwrap();
    venue
        .submit(order(
            "sell",
            Side::Sell,
            OrderType::Limit,
            ten(),
            TimeInForce::Day,
            Some(hundred()),
            None,
            0,
        ))
        .unwrap();

    venue.drain().unwrap();

    let reports = venue.report_records();
    assert_eq!(reports.len(), 4);
    assert_eq!(reports[0].execution_type, ExecutionType::Acknowledged);
    assert_eq!(reports[1].execution_type, ExecutionType::Fill);
    assert_eq!(reports[2].execution_type, ExecutionType::Acknowledged);
    assert_eq!(reports[3].execution_type, ExecutionType::Fill);

    let fills = venue.fill_records();
    assert_eq!(fills.len(), 2);
    assert_eq!(fills[0].order_id, "buy");
    assert_eq!(fills[0].side, Side::Buy);
    assert_eq!(fills[0].price, one0one());
    assert_eq!(fills[1].order_id, "sell");
    assert_eq!(fills[1].price, hundred());
    for report in reports {
        assert_eq!(report.timestamp_ns, STAMP_NS);
    }
    for fill in fills {
        assert_eq!(fill.timestamp_ns, STAMP_NS);
    }
}

#[test]
fn resting_orders_fill_only_when_a_later_quote_touches() {
    let mut venue = venue(0);
    venue
        .on_market(quote(
            "resting",
            0,
            0,
            from_int(99),
            one0one(),
            ten(),
            ten(),
        ))
        .unwrap();
    venue.drain().unwrap();
    venue
        .submit(order(
            "rest-buy",
            Side::Buy,
            OrderType::Limit,
            ten(),
            TimeInForce::Day,
            Some(hundred()),
            None,
            0,
        ))
        .unwrap();
    venue.drain().unwrap();
    assert_eq!(venue.fill_records().len(), 0);

    venue
        .on_market(quote("touch", 1, 1, from_int(99), hundred(), ten(), ten()))
        .unwrap();
    venue.drain().unwrap();
    assert_eq!(venue.fill_records().len(), 1);
    assert_eq!(venue.fill_records()[0].price, hundred());
}

#[test]
fn stop_orders_arm_then_trigger_with_the_triggering_snapshot() {
    let mut venue = venue(0);
    venue.on_market(default_quote("before")).unwrap();
    venue.drain().unwrap();
    venue
        .submit(order(
            "stop-buy",
            Side::Buy,
            OrderType::Stop,
            ten(),
            TimeInForce::Day,
            None,
            Some(one0one()),
            0,
        ))
        .unwrap();
    venue.drain().unwrap();
    assert!(venue.state("stop-buy").unwrap().armed);
    assert_eq!(venue.fill_records().len(), 0);

    venue
        .on_market(quote("trigger", 1, 1, hundred(), one0one(), ten(), ten()))
        .unwrap();
    venue.drain().unwrap();

    let state = venue.state("stop-buy").unwrap();
    assert!(!state.armed);
    assert_eq!(state.triggering_market.unwrap().event_id(), "trigger");
    assert_eq!(state.decision_price, Some(one0one()));
    assert_eq!(venue.fill_records().len(), 1);
    assert_eq!(venue.fill_records()[0].price, one0one());
}

#[test]
fn partial_fill_then_worsening_quotes_never_improve_the_fill() {
    let mut venue = venue(0);
    venue
        .submit(order(
            "partial",
            Side::Buy,
            OrderType::Limit,
            ten(),
            TimeInForce::Day,
            Some(one0one()),
            None,
            0,
        ))
        .unwrap();
    venue
        .on_market(quote(
            "first",
            0,
            1,
            hundred(),
            one0one(),
            ten(),
            from_int(3),
        ))
        .unwrap();
    venue.drain().unwrap();
    assert_eq!(venue.fill_records().len(), 1);
    assert_eq!(venue.fill_records()[0].quantity, from_int(3));

    venue
        .on_market(quote("worse", 1, 2, hundred(), from_int(102), ten(), ten()))
        .unwrap();
    venue.drain().unwrap();
    assert_eq!(venue.fill_records().len(), 1);

    venue
        .on_market(quote(
            "better",
            2,
            3,
            hundred(),
            hundred(),
            ten(),
            from_int(7),
        ))
        .unwrap();
    venue.drain().unwrap();
    assert_eq!(venue.fill_records().len(), 2);
    assert_eq!(venue.fill_records()[1].price, hundred());
}

#[test]
fn stop_limit_arms_on_trigger_and_can_rest_after_a_non_crossing_trigger() {
    let mut venue = venue(0);
    venue
        .submit(order(
            "stop-limit",
            Side::Buy,
            OrderType::StopLimit,
            ten(),
            TimeInForce::Day,
            Some(hundred() + 500_000_000_000_000_000),
            Some(one0one()),
            0,
        ))
        .unwrap();
    venue
        .on_market(quote("trigger", 0, 1, hundred(), one0one(), ten(), ten()))
        .unwrap();
    venue.drain().unwrap();
    assert_eq!(
        venue
            .state("stop-limit")
            .unwrap()
            .triggering_market
            .unwrap()
            .event_id(),
        "trigger"
    );
    assert_eq!(venue.fill_records().len(), 0);

    venue
        .on_market(quote(
            "touch",
            1,
            2,
            hundred(),
            hundred() + 500_000_000_000_000_000,
            ten(),
            ten(),
        ))
        .unwrap();
    venue.drain().unwrap();
    assert_eq!(venue.fill_records().len(), 1);
}

#[test]
fn cancellation_and_replacement_outcomes_are_scheduled() {
    let mut venue = venue(0);
    venue
        .submit(order(
            "cancel",
            Side::Buy,
            OrderType::Limit,
            ten(),
            TimeInForce::Day,
            Some(from_int(99)),
            None,
            0,
        ))
        .unwrap();
    venue.drain().unwrap();
    venue.cancel("cancel").unwrap();
    venue.drain().unwrap();
    assert_eq!(venue.state("cancel").unwrap().status, OrderStatus::Canceled);

    venue
        .submit(order(
            "original",
            Side::Buy,
            OrderType::Limit,
            ten(),
            TimeInForce::Day,
            Some(from_int(99)),
            None,
            0,
        ))
        .unwrap();
    venue.drain().unwrap();
    venue
        .on_market(default_quote("replacement-market"))
        .unwrap();
    venue.drain().unwrap();
    venue
        .replace(
            "original",
            order(
                "replacement",
                Side::Buy,
                OrderType::Limit,
                from_int(4),
                TimeInForce::Day,
                Some(one0one()),
                None,
                0,
            ),
        )
        .unwrap();
    assert_eq!(
        venue.state("original").unwrap().status,
        OrderStatus::Replaced
    );
    venue.drain().unwrap();
    assert_eq!(
        venue.state("replacement").unwrap().status,
        OrderStatus::Filled
    );
    assert!(venue
        .report_records()
        .iter()
        .any(|report| report.execution_type == ExecutionType::Replaced));
}

#[test]
fn ioc_non_fill_expires_during_drain() {
    let mut venue = venue(0);
    venue
        .submit(order(
            "ioc",
            Side::Buy,
            OrderType::Limit,
            ten(),
            TimeInForce::Ioc,
            Some(from_int(99)),
            None,
            0,
        ))
        .unwrap();
    venue
        .on_market(quote(
            "opportunity",
            0,
            1,
            hundred(),
            one0one(),
            ten(),
            ten(),
        ))
        .unwrap();
    venue.drain().unwrap();

    let reports = venue.report_records();
    assert_eq!(reports.len(), 2);
    assert_eq!(reports[0].status, OrderStatus::Accepted);
    assert_eq!(reports[1].status, OrderStatus::Expired);
    assert_eq!(venue.state("ioc").unwrap().status, OrderStatus::Expired);
}

#[test]
fn commands_do_not_inline_outcomes_or_allow_pending_races() {
    let mut venue = venue(MICRO);
    venue
        .submit(order(
            "pending",
            Side::Buy,
            OrderType::Market,
            ten(),
            TimeInForce::Day,
            None,
            None,
            0,
        ))
        .unwrap();
    venue.on_market(default_quote("q")).unwrap();
    // Only the acknowledgement has been dispatched so far.
    venue.drain().unwrap();
    // The order itself is still pending its fill response.
    assert_eq!(venue.cancel("pending"), Err(Error::VenueState));
}
