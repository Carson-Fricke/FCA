//! Owns trader supervision clock; lifecycle coordination remains in the parent.
use std::cell::Cell;
use std::rc::Rc;

/// Injected monotonic wall clock.
pub trait SupervisionClock {
    /// Current wall-clock time in nanoseconds.
    fn now_ns(&self) -> i64;
}

/// A wall clock backed by `std::time::SystemTime`.
#[derive(Clone, Copy, Debug, Default)]
pub struct SystemClock;

impl SupervisionClock for SystemClock {
    fn now_ns(&self) -> i64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|duration| duration.as_nanos() as i64)
            .unwrap_or_default()
    }
}

/// A manually advanced clock for deterministic tests.
#[derive(Clone, Debug, Default)]
pub struct ManualClock {
    now_ns: Rc<Cell<i64>>,
}

impl ManualClock {
    /// Build a manual clock at `now_ns`.
    #[must_use]
    pub fn new(now_ns: i64) -> Self {
        Self {
            now_ns: Rc::new(Cell::new(now_ns)),
        }
    }

    /// Advance the clock by `delta_ns`.
    pub fn advance(&self, delta_ns: i64) {
        self.now_ns.set(self.now_ns.get() + delta_ns);
    }

    /// Set the clock to an absolute instant.
    pub fn set(&self, now_ns: i64) {
        self.now_ns.set(now_ns);
    }
}

impl SupervisionClock for ManualClock {
    fn now_ns(&self) -> i64 {
        self.now_ns.get()
    }
}
