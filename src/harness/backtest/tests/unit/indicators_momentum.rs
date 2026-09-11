use super::super::base::{Tick, TickIndicator, TickKind};
use super::*;

fn trade(sequence: u64, price: i64) -> Tick {
    Tick {
        timestamp_ns: sequence as i64,
        sequence,
        kind: TickKind::Trade {
            price: Decimal18::from_i64(price),
            quantity: Decimal18::from_i64(1),
        },
    }
}

#[test]
fn rsi_warms_up_after_period_plus_one_trades() {
    let mut rsi = RelativeStrengthIndex::new(3).expect("rsi");
    for (sequence, price) in [(0u64, 1i64), (1, 2), (2, 3)] {
        rsi.on_tick(&trade(sequence, price)).expect("tick");
    }
    assert!(rsi.value().is_none());
    rsi.on_tick(&trade(3, 4)).expect("tick");
    // All gains: average loss is zero, so RSI is 100.
    assert_eq!(rsi.value().expect("value"), hundred());
}

#[test]
fn flat_stream_maps_to_one_hundred() {
    let mut rsi = RelativeStrengthIndex::new(2).expect("rsi");
    for sequence in 0..4 {
        rsi.on_tick(&trade(sequence, 5)).expect("tick");
    }
    assert_eq!(rsi.value().expect("value"), hundred());
}

#[test]
fn ordering_is_enforced() {
    let mut rsi = RelativeStrengthIndex::new(2).expect("rsi");
    rsi.on_tick(&trade(5, 1)).expect("tick");
    assert!(rsi.on_tick(&trade(4, 1)).is_err());
}
