use super::{ClockError, SimClock};

/// 2024-01-19T14:30:00Z in epoch nanoseconds.
const STAMP_NS: i128 = 1_705_674_600_000_000_000;

#[test]
fn sim_clock_advances_only_from_explicit_monotonic_events() {
    let mut sim = SimClock::new(STAMP_NS);
    assert_eq!(sim.now(), STAMP_NS);

    assert_eq!(sim.advance_by(1_000), Ok(STAMP_NS + 1_000));
    assert_eq!(sim.now(), STAMP_NS + 1_000);

    assert_eq!(
        sim.advance_to(STAMP_NS),
        Err(ClockError::RetrogradeTimestamp)
    );
    assert_eq!(sim.now(), STAMP_NS + 1_000);

    assert_eq!(sim.advance_by(-1_000), Err(ClockError::NegativeDelta));
    assert_eq!(sim.now(), STAMP_NS + 1_000);

    // Advancing to the current head is a no-op.
    assert_eq!(sim.advance_to(STAMP_NS + 1_000), Ok(STAMP_NS + 1_000));
}
