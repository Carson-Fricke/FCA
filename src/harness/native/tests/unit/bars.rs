use super::{bucket_start_ns, build_bars, interval_delta_ns, BarAggregator, Error, NS_PER_SECOND};
use crate::decimal::{add, from_int, ONE};
use crate::market::TradeEvent;

const STAMP_NS: i128 = 1_705_674_600_000_000_000;
const MINUTE_NS: i128 = 60 * NS_PER_SECOND;

fn golden_trade(
    event_id: &'static str,
    offset_seconds: i64,
    sequence: u64,
    price: i128,
    quantity: i128,
) -> TradeEvent<'static> {
    TradeEvent {
        event_id,
        timestamp_ns: STAMP_NS + offset_seconds as i128 * NS_PER_SECOND,
        sequence,
        symbol: "AAPL",
        price,
        quantity,
        venue: None,
        trade_id: None,
    }
}

#[test]
fn golden_bars_reconcile_prices_volume_and_boundaries_exactly() {
    let trades = [
        golden_trade(
            "t3",
            61,
            3,
            from_int(101) + 250_000_000_000_000_000,
            750_000_000_000_000_000,
        ),
        golden_trade(
            "t1",
            0,
            1,
            from_int(100) + 100_000_000_000_000_000,
            from_int(2) + 500_000_000_000_000_000,
        ),
        golden_trade(
            "t2",
            30,
            2,
            from_int(99) + 900_000_000_000_000_000,
            from_int(1) + 250_000_000_000_000_000,
        ),
        golden_trade("t4", 120, 4, from_int(102), from_int(3)),
    ];

    let bars = build_bars(&trades, MINUTE_NS, None).unwrap();
    assert_eq!(bars.len(), 3);
    let starts = [STAMP_NS, STAMP_NS + MINUTE_NS, STAMP_NS + 2 * MINUTE_NS];
    let ends = [
        STAMP_NS + MINUTE_NS,
        STAMP_NS + 2 * MINUTE_NS,
        STAMP_NS + 3 * MINUTE_NS,
    ];
    for (index, bar) in bars.iter().enumerate() {
        assert_eq!(bar.start_ns, starts[index]);
        assert_eq!(bar.end_ns, ends[index]);
    }

    assert_eq!(bars[0].open, from_int(100) + 100_000_000_000_000_000);
    assert_eq!(bars[0].high, from_int(100) + 100_000_000_000_000_000);
    assert_eq!(bars[0].low, from_int(99) + 900_000_000_000_000_000);
    assert_eq!(bars[0].close, from_int(99) + 900_000_000_000_000_000);
    assert_eq!(bars[0].volume, from_int(3) + 750_000_000_000_000_000);
    assert_eq!(bars[0].trade_count, 2);

    assert_eq!(bars[1].open, from_int(101) + 250_000_000_000_000_000);
    assert_eq!(bars[1].close, from_int(101) + 250_000_000_000_000_000);
    assert_eq!(bars[1].volume, 750_000_000_000_000_000);
    assert_eq!(bars[1].trade_count, 1);

    assert_eq!(bars[2].open, from_int(102));
    assert_eq!(bars[2].close, from_int(102));
    assert_eq!(bars[2].volume, from_int(3));

    let mut total_volume: i128 = 0;
    let mut total_quantity: i128 = 0;
    for bar in &bars {
        total_volume = add(total_volume, bar.volume).unwrap();
    }
    for trade in &trades {
        total_quantity = add(total_quantity, trade.quantity).unwrap();
    }
    assert_eq!(total_volume, total_quantity);
}

#[test]
fn streaming_aggregator_keeps_one_symbol_per_instance() {
    let mut aggregator = BarAggregator::new(5 * MINUTE_NS, Some("AAPL")).unwrap();
    aggregator
        .add_many(&[
            golden_trade("t1", 0, 1, from_int(100), ONE),
            golden_trade("t2", 1, 2, from_int(101), from_int(2)),
        ])
        .unwrap();

    let bars = aggregator.finalize().unwrap();
    assert_eq!(bars.len(), 1);
    let bar = bars[0];
    assert_eq!(bar.timestamp_ns(), STAMP_NS);
    assert_eq!(bar.open, from_int(100));
    assert_eq!(bar.close, from_int(101));
    assert_eq!(bar.volume, from_int(3));
    assert_eq!(aggregator.symbol(), Some("AAPL"));

    let wrong_symbol = TradeEvent {
        event_id: "t3",
        timestamp_ns: STAMP_NS,
        sequence: 3,
        symbol: "MSFT",
        price: ONE,
        quantity: ONE,
        venue: None,
        trade_id: None,
    };
    assert_eq!(aggregator.add(wrong_symbol), Err(Error::InvalidArgument));
}

#[test]
fn interval_deltas_parse_the_configuration_vocabulary() {
    assert_eq!(interval_delta_ns("1s"), Ok(NS_PER_SECOND));
    assert_eq!(interval_delta_ns("5min"), Ok(5 * 60 * NS_PER_SECOND));
    assert_eq!(interval_delta_ns("1m"), Ok(60 * NS_PER_SECOND));
    assert_eq!(interval_delta_ns(" 1MIN "), Ok(60 * NS_PER_SECOND));
    assert_eq!(interval_delta_ns("1h"), Ok(3600 * NS_PER_SECOND));
    assert_eq!(interval_delta_ns("1d"), Ok(86_400 * NS_PER_SECOND));
    assert_eq!(interval_delta_ns("0min"), Err(Error::InvalidArgument));
    assert_eq!(interval_delta_ns("min"), Err(Error::InvalidArgument));
    assert_eq!(interval_delta_ns("1w"), Err(Error::InvalidArgument));
    assert_eq!(interval_delta_ns(""), Err(Error::InvalidArgument));
    assert!(matches!(
        BarAggregator::new(0, None),
        Err(Error::InvalidArgument)
    ));
}

#[test]
fn bucket_boundaries_are_half_open_and_floored_from_the_epoch() {
    let boundary = bucket_start_ns(STAMP_NS, MINUTE_NS);
    assert_eq!(boundary, STAMP_NS);
    assert_eq!(
        bucket_start_ns(STAMP_NS + MINUTE_NS, MINUTE_NS),
        STAMP_NS + MINUTE_NS
    );
    assert_eq!(
        bucket_start_ns(STAMP_NS + MINUTE_NS - 1, MINUTE_NS),
        STAMP_NS
    );
    assert_eq!(bucket_start_ns(-1, MINUTE_NS), -MINUTE_NS);
}
