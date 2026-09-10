//! Injectable clock for job-record timestamps.

use chrono::{DateTime, Utc};

/// Clock boundary; deterministic tests supply a fixed implementation.
pub trait Clock: Send + Sync {
    /// Return the current UTC instant.
    fn now(&self) -> DateTime<Utc>;
}

/// Production clock backed by the system time.
#[derive(Debug, Clone, Copy, Default)]
pub struct SystemClock;

impl Clock for SystemClock {
    fn now(&self) -> DateTime<Utc> {
        Utc::now()
    }
}
