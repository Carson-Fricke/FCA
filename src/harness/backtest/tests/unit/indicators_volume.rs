use super::super::base::{
    Tick, TickIndicator, TickKind, DAY_NS, DEFAULT_BUCKET_NS, MIDNIGHT_UTC_NS,
};
use super::*;

fn trade(timestamp_ns: i64, sequence: u64, price: i64, quantity: i64) -> Tick {
    Tick {
        timestamp_ns,
        sequence,
        kind: TickKind::Trade {
            price: Decimal18::from_i64(price),
            quantity: Decimal18::from_i64(quantity),
        },
    }
}

fn vwap() -> VolumeWeightedAveragePrice {
    IndicatorMachine::with_kernel(
        VwapKernel::new(),
        "SPY",
        DEFAULT_BUCKET_NS,
        Some(MIDNIGHT_UTC_NS),
    )
    .expect("vwap")
}

#[test]
fn vwap_is_notional_over_volume() {
    let mut vwap = vwap();
    vwap.on_tick(&trade(0, 0, 10, 2)).expect("tick");
    vwap.on_tick(&trade(1, 1, 20, 1)).expect("tick");
    // (10*2 + 20*1) / 3 = 40/3 = 13.333...
    assert!(vwap
        .value()
        .expect("value")
        .to_string()
        .starts_with("13.333"));
}

#[test]
fn vwap_resets_each_session() {
    let mut vwap = vwap();
    let day = 19_723 * DAY_NS;
    vwap.on_tick(&trade(day, 0, 10, 1)).expect("tick");
    assert!(vwap.value().is_some());
    vwap.on_tick(&trade(day + DAY_NS, 1, 10, 1)).expect("tick");
    assert!(vwap.value().is_some());
    // A fresh session yields the first trade's price, not a blended one.
    assert_eq!(vwap.value().expect("value"), Decimal18::from_i64(10));
}

#[test]
fn dollar_volume_cumulative_and_rolling() {
    let mut cumulative = IndicatorMachine::with_kernel(
        DollarVolumeKernel::new(None).expect("dv"),
        "SPY",
        DEFAULT_BUCKET_NS,
        None,
    )
    .expect("dv");
    cumulative.on_tick(&trade(0, 0, 10, 2)).expect("tick");
    cumulative.on_tick(&trade(1, 1, 20, 1)).expect("tick");
    assert_eq!(cumulative.value().expect("value"), Decimal18::from_i64(40));

    let mut rolling = IndicatorMachine::with_kernel(
        DollarVolumeKernel::new(Some(60_000_000_000)).expect("dv"),
        "SPY",
        DEFAULT_BUCKET_NS,
        None,
    )
    .expect("dv");
    rolling.on_tick(&trade(0, 0, 10, 1)).expect("tick");
    // Rotating two buckets later and flushing expires the first trade.
    rolling
        .on_tick(&trade(120_000_000_000, 1, 10, 1))
        .expect("tick");
    rolling.flush();
    assert_eq!(rolling.value().expect("value"), Decimal18::from_i64(10));
}

#[test]
fn obv_signs_against_the_previous_trade() {
    let mut obv = IndicatorMachine::with_kernel(ObvKernel::new(), "SPY", DEFAULT_BUCKET_NS, None)
        .expect("obv");
    obv.on_tick(&trade(0, 0, 10, 5)).expect("tick");
    assert_eq!(obv.value(), Some(Decimal18::ZERO));
    obv.on_tick(&trade(1, 1, 11, 2)).expect("tick");
    assert_eq!(obv.value(), Some(Decimal18::from_i64(2)));
    // A flat trade repeats the previous sign.
    obv.on_tick(&trade(2, 2, 11, 3)).expect("tick");
    assert_eq!(obv.value(), Some(Decimal18::from_i64(5)));
    obv.on_tick(&trade(3, 3, 9, 4)).expect("tick");
    assert_eq!(obv.value(), Some(Decimal18::from_i64(1)));
}
