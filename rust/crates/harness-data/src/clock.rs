//! Engine clock boundary.
//!
//! The data layer never reads wall time. Reads are clipped to an injected
//! clock head, exactly like the Python `harness.core.clock.Clock` boundary.

use std::cell::Cell;

use crate::time::DateTime;

/// The read boundary shared by point-in-time views.
pub trait Clock {
    /// The current engine instant, the hard upper bound for every read.
    fn now(&self) -> DateTime;
}

impl Clock for Box<dyn Clock> {
    fn now(&self) -> DateTime {
        (**self).now()
    }
}

/// A manually advanced clock for deterministic tests and replay shells.
#[derive(Debug)]
pub struct ManualClock {
    now: Cell<DateTime>,
}

impl ManualClock {
    /// Build a clock frozen at `now`.
    pub fn new(now: DateTime) -> Self {
        Self {
            now: Cell::new(now),
        }
    }

    /// Move the head to `now`.
    pub fn set(&self, now: DateTime) {
        self.now.set(now);
    }
}

impl Default for ManualClock {
    fn default() -> Self {
        Self::new(DateTime::EPOCH)
    }
}

impl Clock for ManualClock {
    fn now(&self) -> DateTime {
        self.now.get()
    }
}
