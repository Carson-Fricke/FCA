//! Process-wide request-budget governor.
//!
//! One governor is shared as an `Arc` by every ensure job so retries and
//! concurrent jobs consume the same `requests_per_minute` budget. The token
//! bucket starts full with a single token: the first acquire is immediate and
//! every later acquire reserves the next evenly spaced slot on the tokio time
//! line, which keeps sustained throughput at the configured rate instead of
//! bursting and waiting out 429 backoff.

use std::sync::{Arc, Mutex, PoisonError};
use std::time::Duration;

use tokio::time::Instant;

/// Evenly spaced request budget derived from `requests_per_minute`.
pub struct RequestGovernor {
    interval: Option<Duration>,
    next_allowed: Mutex<Instant>,
    requests_per_minute: Option<u32>,
}

impl RequestGovernor {
    /// Build a shared governor; `None` (or zero) makes [`acquire`] a no-op.
    ///
    /// [`acquire`]: RequestGovernor::acquire
    pub fn new(requests_per_minute: Option<u32>) -> Arc<Self> {
        let interval = requests_per_minute
            .filter(|requests| *requests > 0)
            .map(|requests| Duration::from_secs_f64(60.0 / f64::from(requests)));
        Arc::new(Self {
            interval,
            next_allowed: Mutex::new(Instant::now()),
            requests_per_minute,
        })
    }

    /// Wait until the next budget slot and consume it.
    pub async fn acquire(&self) {
        let Some(interval) = self.interval else {
            return;
        };
        let wait = {
            let mut next_allowed = self
                .next_allowed
                .lock()
                .unwrap_or_else(PoisonError::into_inner);
            let now = Instant::now();
            let slot = (*next_allowed).max(now);
            *next_allowed = slot + interval;
            slot.saturating_duration_since(now)
        };
        if !wait.is_zero() {
            tokio::time::sleep(wait).await;
        }
    }

    /// The budget exactly as supplied to [`RequestGovernor::new`].
    pub fn requests_per_minute(&self) -> Option<u32> {
        self.requests_per_minute
    }
}

#[cfg(test)]
#[path = "../tests/unit/governor.rs"]
mod tests;
