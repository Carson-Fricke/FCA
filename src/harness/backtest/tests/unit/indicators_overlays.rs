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

fn quote(sequence: u64, bid: i64, ask: i64) -> Tick {
    Tick {
        timestamp_ns: sequence as i64,
        sequence,
        kind: TickKind::Quote {
            bid: Decimal18::from_i64(bid),
            ask: Decimal18::from_i64(ask),
        },
    }
}

#[test]
fn sma_warms_up_and_rolls() {
    let mut sma = SimpleMovingAverage::new(2).expect("sma");
    sma.on_tick(&trade(0, 10)).expect("tick");
    assert!(sma.value().is_none());
    sma.on_tick(&trade(1, 20)).expect("tick");
    assert_eq!(sma.value().expect("value"), Decimal18::from_i64(15));
    sma.on_tick(&trade(2, 30)).expect("tick");
    assert_eq!(sma.value().expect("value"), Decimal18::from_i64(25));
}

#[test]
fn ema_seeds_and_warms_up() {
    let mut ema = ExponentialMovingAverage::new(2).expect("ema");
    ema.on_tick(&trade(0, 10)).expect("tick");
    assert!(ema.value().is_none());
    ema.on_tick(&trade(1, 20)).expect("tick");
    // alpha = 2/3, ema = 10 + (20-10)*2/3 = 16.666...
    let value = ema.value().expect("value");
    assert!(value.to_string().starts_with("16.666"));
}

#[test]
fn mid_sma_uses_quote_midpoints() {
    let mut sma = MidSimpleMovingAverage::new(2).expect("mid_sma");
    sma.on_tick(&quote(0, 100, 102)).expect("tick");
    sma.on_tick(&trade(1, 500)).expect("tick");
    assert!(sma.value().is_none(), "one quote is not enough");
    sma.on_tick(&quote(2, 200, 204)).expect("tick");
    // mids: 101 and 202 -> mean 151.5
    let value = sma.value().expect("value");
    assert_eq!(value.to_string(), "151.500000000000000000");
}
