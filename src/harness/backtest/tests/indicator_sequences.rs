//! Native indicator outputs preserve warm-up and final partial windows.
use backtest::indicators::overlays::EmaKernel;
use backtest::indicators::{IndicatorMachine, Tick, TickIndicator, TickKind};
use num::Decimal18;
fn trade(time: i64, sequence: u64, price: i64) -> Tick {
    Tick {
        timestamp_ns: time,
        sequence,
        kind: TickKind::Trade {
            price: Decimal18::from_i64(price),
            quantity: Decimal18::from_i64(1),
        },
    }
}
#[test]
fn native_sequences_preserve_undefined_buckets_and_causal_availability() {
    let mut indicator =
        IndicatorMachine::with_kernel(EmaKernel::new(2).unwrap(), "spy", 10, None).unwrap();
    indicator.on_tick(&trade(1, 1, 10)).unwrap();
    assert!(indicator.samples().is_empty());
    assert_eq!(indicator.in_progress().unwrap().value, None);
    indicator.on_tick(&trade(10, 2, 20)).unwrap();
    assert_eq!(indicator.samples().len(), 1);
    assert_eq!(indicator.samples()[0].value, None);
    assert_eq!(indicator.samples()[0].available_at_ns, Some(10));
    assert_eq!(indicator.samples()[0].symbol, "SPY");
    let open = indicator.in_progress().unwrap();
    assert_eq!(
        (open.start_ns, open.end_ns, open.available_at_ns),
        (10, 20, None)
    );
    assert!(open.value.is_some());
    assert_eq!(open.open, open.value);
    indicator.advance(19).unwrap();
    assert_eq!(indicator.samples().len(), 1);
    indicator.advance(20).unwrap();
    assert_eq!(indicator.samples().len(), 2);
    assert!(indicator.in_progress().is_none());
    assert!(indicator.on_tick(&trade(19, 3, 30)).is_err());
    indicator.on_tick(&trade(40, 4, 30)).unwrap();
    assert_eq!(indicator.samples().len(), 2); // no invented empty buckets
}
