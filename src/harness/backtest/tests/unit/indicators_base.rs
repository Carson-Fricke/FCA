use super::*;

struct Accumulator {
    window: RollingWindow,
}

impl Accumulator {
    fn new(period: i64) -> Self {
        Self {
            window: RollingWindow::new(period).expect("window"),
        }
    }
}

impl IndicatorKernel for Accumulator {
    fn name(&self) -> &'static str {
        "accumulator"
    }

    fn drivers(&self) -> &'static [TickDriver] {
        &[TickDriver::Trade]
    }

    fn on_trade(&mut self, price: Decimal18, _quantity: Decimal18, _timestamp_ns: i64) {
        self.window.push(price);
    }

    fn value(&self) -> Option<Decimal18> {
        self.window.mean()
    }
}

fn trade(timestamp_ns: i64, sequence: u64, price: i64) -> Tick {
    Tick {
        timestamp_ns,
        sequence,
        kind: TickKind::Trade {
            price: Decimal18::from_i64(price),
            quantity: Decimal18::from_i64(1),
        },
    }
}

#[test]
fn bucket_start_floors_to_the_grid() {
    assert_eq!(bucket_start_ns(0, 60_000_000_000), 0);
    assert_eq!(bucket_start_ns(59_999_999_999, 60_000_000_000), 0);
    assert_eq!(
        bucket_start_ns(60_000_000_000, 60_000_000_000),
        60_000_000_000
    );
    assert_eq!(
        bucket_start_ns(125_000_000_000, 60_000_000_000),
        120_000_000_000
    );
    // Negative timestamps floor toward negative infinity.
    assert_eq!(bucket_start_ns(-1, 60_000_000_000), -60_000_000_000);
}

#[test]
fn ordering_guard_rejects_regressions() {
    let mut guard = OrderGuard::default();
    assert!(guard.accept(10, 1).is_ok());
    assert!(guard.accept(10, 2).is_ok());
    assert!(guard.accept(9, 5).is_err());
    assert!(guard.accept(10, 1).is_err());
}

#[test]
fn session_start_rolls_at_midnight() {
    // 2024-01-01T00:00:00Z exactly.
    let day = 19_723 * DAY_NS;
    assert_eq!(session_start_ns(day, MIDNIGHT_UTC_NS), day);
    assert_eq!(session_start_ns(day + DAY_NS - 1, MIDNIGHT_UTC_NS), day);
    assert_eq!(
        session_start_ns(day + DAY_NS, MIDNIGHT_UTC_NS),
        day + DAY_NS
    );
    // An anchor at noon keeps an 11:00 event in the previous session.
    let noon = DAY_NS / 2;
    assert_eq!(session_start_ns(day + noon - 1, noon), day - DAY_NS + noon);
    assert_eq!(session_start_ns(day + noon, noon), day + noon);
}

#[test]
fn machine_emits_one_sample_per_non_empty_bucket() {
    let mut machine =
        IndicatorMachine::with_kernel(Accumulator::new(1), "spy", DEFAULT_BUCKET_NS, None)
            .expect("machine");
    machine.on_tick(&trade(0, 0, 10)).expect("tick");
    machine.on_tick(&trade(1, 1, 20)).expect("tick");
    // A gap bucket with no driver event emits nothing.
    machine
        .on_tick(&trade(2 * DEFAULT_BUCKET_NS, 2, 30))
        .expect("tick");
    machine.flush();
    let samples = machine.samples().to_vec();
    assert_eq!(samples.len(), 2);
    assert_eq!(samples[0].start_ns, 0);
    assert_eq!(samples[0].end_ns, DEFAULT_BUCKET_NS);
    assert_eq!(samples[1].start_ns, 2 * DEFAULT_BUCKET_NS);
    assert_eq!(samples[0].symbol, "SPY");
}

#[test]
fn machine_flush_closes_the_trailing_bucket() {
    let mut machine =
        IndicatorMachine::with_kernel(Accumulator::new(1), "spy", DEFAULT_BUCKET_NS, None)
            .expect("machine");
    machine.on_tick(&trade(500, 0, 10)).expect("tick");
    machine.flush();
    assert_eq!(machine.samples().len(), 1);
    // Flush is idempotent.
    machine.flush();
    assert_eq!(machine.samples().len(), 1);
}

#[test]
fn machine_resets_state_on_session_rollover() {
    #[derive(Default)]
    struct Counter {
        count: i64,
    }
    impl IndicatorKernel for Counter {
        fn name(&self) -> &'static str {
            "counter"
        }
        fn drivers(&self) -> &'static [TickDriver] {
            &[TickDriver::Trade]
        }
        fn on_trade(&mut self, _p: Decimal18, _q: Decimal18, _t: i64) {
            self.count += 1;
        }
        fn reset_session(&mut self) {
            self.count = 0;
        }
        fn value(&self) -> Option<Decimal18> {
            Some(Decimal18::from_i64(self.count))
        }
    }
    let mut machine = IndicatorMachine::with_kernel(
        Counter::default(),
        "spy",
        DEFAULT_BUCKET_NS,
        Some(MIDNIGHT_UTC_NS),
    )
    .expect("machine");
    let day = 19_723 * DAY_NS;
    machine.on_tick(&trade(day, 0, 10)).expect("tick");
    machine.on_tick(&trade(day + 1, 1, 10)).expect("tick");
    assert_eq!(machine.value(), Some(Decimal18::from_i64(2)));
    machine.on_tick(&trade(day + DAY_NS, 2, 10)).expect("tick");
    assert_eq!(machine.value(), Some(Decimal18::from_i64(1)));
}
