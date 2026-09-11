use super::super::base::{Tick, TickIndicator, TickKind, DAY_NS};
use super::*;

fn trade(timestamp_ns: i64, sequence: u64, price: &str) -> Tick {
    Tick {
        timestamp_ns,
        sequence,
        kind: TickKind::Trade {
            price: price.parse().expect("price"),
            quantity: Decimal18::from_i64(1),
        },
    }
}

#[test]
fn atr_warms_up_over_trade_buckets() {
    let mut atr = AverageTrueRange::new(2).expect("atr");
    // Bucket 0: high 12, low 10, close 11 => range 2.
    atr.on_tick(&trade(0, 0, "10")).expect("tick");
    atr.on_tick(&trade(1, 1, "12")).expect("tick");
    atr.on_tick(&trade(2, 2, "11")).expect("tick");
    // Rotate into bucket 1: high 15, low 9, close 14.
    atr.on_tick(&trade(60_000_000_000, 3, "15")).expect("tick");
    atr.on_tick(&trade(60_000_000_001, 4, "9")).expect("tick");
    assert!(atr.value().is_none(), "one range is not enough");
    // Rotate into bucket 2: warm-up completes with the second range.
    atr.on_tick(&trade(120_000_000_000, 5, "20")).expect("tick");
    let value = atr.value().expect("value");
    // Range 1 = 2. Range 2 = max(20-15=5, |15-11|=4, |15-9|... ) = 6.
    // Seed average = (2 + 6) / 2 = 4.
    assert_eq!(value.to_string(), "4.000000000000000000");
}

#[test]
fn realized_vol_is_zero_for_a_single_return_and_warms_on_two() {
    let mut vol = RealizedVolatility::new(60_000_000_000).expect("vol");
    vol.on_tick(&trade(0, 0, "100")).expect("tick");
    assert!(vol.value().is_none(), "no return yet");
    vol.on_tick(&trade(1, 1, "110")).expect("tick");
    // One return of 0.1 => population stddev 0.
    assert_eq!(vol.value(), Some(Decimal18::ZERO));
    vol.on_tick(&trade(2, 2, "99")).expect("tick");
    assert!(vol.value().expect("value").is_positive());
}

#[test]
fn realized_vol_resets_on_session_rollover() {
    let mut vol = RealizedVolatility::new(DAY_NS).expect("vol");
    let day = 19_723 * DAY_NS;
    vol.on_tick(&trade(day, 0, "100")).expect("tick");
    vol.on_tick(&trade(day + 1, 1, "110")).expect("tick");
    assert!(vol.value().is_some());
    vol.on_tick(&trade(day + DAY_NS, 2, "120")).expect("tick");
    // The session reset clears the previous price, so one trade emits nothing.
    assert!(vol.value().is_none());
}
