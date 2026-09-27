//! Adaptive per-key request lane.
//!
//! One lane per API key owns two things:
//!
//! 1. A governor-facing in-flight bound `C` that the fetch scheduler honours.
//!    The bound is adapted by a BBR-shaped controller: AIMD probes for the
//!    highest sustainable request rate the environment allows (bandwidth, RTT,
//!    or provider), while Little's law (`C = rate x latency`) holds the
//!    operating point and a latency guard breaks the overshoot feedback.
//! 2. In-memory calibration shared by every client built on the same key, so
//!    concurrent jobs learn one lane's ceiling together.
//!
//! The hard limits are the only fixed numbers: Alpaca's requests-per-minute
//! budget (the governor) and the 10k rows per request (the fetch planner).

use std::collections::{HashMap, VecDeque};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use tokio::sync::Notify;

/// Absolute p95 ceiling in seconds: protects against uniform saturation where
/// every request slows down together, and keeps requests far from the 30s
/// client timeout. Skew-based signals are deliberately absent: the provider's
/// per-page service time varies several-fold with the data itself (dense
/// versus sparse pages), which is workload noise, not queueing.
const LATENCY_CEILING_SECONDS: f64 = 10.0;
/// Ramp gain while throughput is still improving.
const RAMP_FACTOR: f64 = 2.0;
/// Baseline gain above the bandwidth-delay product.
const PROBE_FACTOR: f64 = 1.25;
/// First-strike backoff: a single stressed window trims the bound gently.
const BACKOFF_GENTLE: f64 = 0.85;
/// Consecutive stressed windows before falling back to the last known knee.
const BACKOFF_STRIKES: u32 = 3;
/// How many recent window p50s define the min-latency (no-queue) baseline.
const L_MIN_WINDOWS: usize = 10;
/// Slow decay of the best-throughput estimate so a moved network re-ramps.
const T_BEST_DECAY: f64 = 0.98;
/// Window closes after this many completions and at least `window_min`.
const WINDOW_COMPLETIONS: u32 = 32;
/// Ring capacity for latency samples inside one window.
const LATENCY_SAMPLES: usize = 256;

/// Tunable cadence for the controller (short values in unit tests).
#[derive(Clone, Copy, Debug)]
pub struct LaneTuning {
    /// Minimum window length.
    pub window_min: Duration,
    /// Maximum window length, forced closed even without completions flowing.
    pub window_max: Duration,
    /// Completions needed to close a window early.
    pub window_completions: u32,
    /// Initial in-flight bound before any measurement.
    pub initial_bound: usize,
}

impl Default for LaneTuning {
    fn default() -> Self {
        Self {
            window_min: Duration::from_secs(1),
            window_max: Duration::from_secs(5),
            window_completions: WINDOW_COMPLETIONS,
            initial_bound: 32,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Mode {
    Ramp,
    Hold,
    Probe,
    Backoff,
}

#[derive(Debug, Default)]
struct Window {
    started: Option<Instant>,
    completions: u32,
    latencies: Vec<f64>,
}

#[derive(Debug)]
struct LaneState {
    window: Window,
    previous_rate: f64,
    t_best: f64,
    l_min: f64,
    recent_p50: VecDeque<f64>,
    best_bound: usize,
    bad_windows: u32,
    windows: u64,
    mode: Mode,
    tuning: LaneTuning,
}

impl LaneState {
    fn new(tuning: LaneTuning) -> Self {
        Self {
            window: Window::default(),
            previous_rate: 0.0,
            t_best: 0.0,
            l_min: f64::INFINITY,
            recent_p50: VecDeque::new(),
            best_bound: 0,
            bad_windows: 0,
            windows: 0,
            mode: Mode::Ramp,
            tuning,
        }
    }
}

/// One API-key lane: adaptive in-flight bound plus its limiter.
pub struct Lane {
    ceiling: usize,
    rate_ceiling: Option<f64>,
    bound: AtomicUsize,
    in_flight: AtomicUsize,
    notify: Notify,
    state: Mutex<LaneState>,
}

impl std::fmt::Debug for Lane {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("Lane")
            .field("ceiling", &self.ceiling)
            .field("rate_ceiling", &self.rate_ceiling)
            .field("bound", &self.bound())
            .field("in_flight", &self.in_flight())
            .finish()
    }
}

/// A held in-flight slot; dropping it wakes one waiter.
pub struct LanePermit<'a> {
    lane: &'a Lane,
}

impl Drop for LanePermit<'_> {
    fn drop(&mut self) {
        self.lane.in_flight.fetch_sub(1, Ordering::AcqRel);
        self.lane.notify.notify_waiters();
    }
}

impl Lane {
    /// Build a lane around an optional requests-per-minute budget.
    pub fn new(requests_per_minute: Option<u32>, ceiling: usize) -> Self {
        let ceiling = ceiling.max(1);
        let tuning = LaneTuning::default();
        let initial = tuning.initial_bound.min(ceiling).max(1);
        Self {
            ceiling,
            rate_ceiling: requests_per_minute.map(|rpm| f64::from(rpm) / 60.0),
            bound: AtomicUsize::new(initial),
            in_flight: AtomicUsize::new(0),
            notify: Notify::new(),
            state: Mutex::new(LaneState::new(tuning)),
        }
    }

    /// Build a lane with an explicit controller cadence (unit tests).
    #[must_use]
    pub fn with_tuning(
        requests_per_minute: Option<u32>,
        ceiling: usize,
        tuning: LaneTuning,
    ) -> Self {
        let ceiling = ceiling.max(1);
        let initial = tuning.initial_bound.min(ceiling).max(1);
        Self {
            ceiling,
            rate_ceiling: requests_per_minute.map(|rpm| f64::from(rpm) / 60.0),
            bound: AtomicUsize::new(initial),
            in_flight: AtomicUsize::new(0),
            notify: Notify::new(),
            state: Mutex::new(LaneState::new(tuning)),
        }
    }

    /// The current adaptive in-flight bound.
    pub fn bound(&self) -> usize {
        self.bound.load(Ordering::Acquire)
    }

    /// Requests currently holding a slot.
    pub fn in_flight(&self) -> usize {
        self.in_flight.load(Ordering::Acquire)
    }

    /// Wait for an in-flight slot under the current adaptive bound.
    pub async fn acquire(&self) -> LanePermit<'_> {
        loop {
            let current = self.in_flight.load(Ordering::Acquire);
            if current < self.bound.load(Ordering::Acquire)
                && self
                    .in_flight
                    .compare_exchange(current, current + 1, Ordering::AcqRel, Ordering::Acquire)
                    .is_ok()
            {
                return LanePermit { lane: self };
            }
            // Register interest before re-checking so a release between the
            // check and the await is not lost.
            let notified = self.notify.notified();
            if self.in_flight.load(Ordering::Acquire) < self.bound.load(Ordering::Acquire) {
                continue;
            }
            notified.await;
        }
    }

    /// Record one completed request's service latency (send to body complete).
    pub fn complete(&self, latency: Duration) {
        let now = Instant::now();
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let tuning = state.tuning;
        let started = {
            let window = &mut state.window;
            let started = *window.started.get_or_insert(now);
            window.completions += 1;
            window.latencies.push(latency.as_secs_f64());
            if window.latencies.len() > LATENCY_SAMPLES {
                window.latencies.remove(0);
            }
            started
        };
        let elapsed = now.saturating_duration_since(started);
        let cadence_met =
            tuning.window_min <= elapsed && state.window.completions >= tuning.window_completions;
        if cadence_met || elapsed >= tuning.window_max {
            self.close_window(&mut state, elapsed);
        }
    }

    /// Feed one pre-measured window straight to the control law (unit tests).
    #[cfg(test)]
    fn observe_window(&self, completions: u32, elapsed: Duration, latencies: &[Duration]) {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        state.window = Window {
            started: None,
            completions,
            latencies: latencies.iter().map(Duration::as_secs_f64).collect(),
        };
        self.close_window(&mut state, elapsed);
    }

    fn close_window(&self, state: &mut LaneState, elapsed: Duration) {
        let window = std::mem::take(&mut state.window);
        if window.completions == 0 || elapsed.is_zero() {
            return;
        }
        let seconds = elapsed.as_secs_f64();
        let rate = f64::from(window.completions) / seconds;
        let p50 = percentile(&window.latencies, 0.50);
        let p95 = percentile(&window.latencies, 0.95);

        let previous = state.previous_rate;
        state.previous_rate = rate;
        // Min-latency baseline: the bandwidth-delay product must use the
        // no-queue latency, never the current one, or the inflated latency of
        // an overfull pipe feeds back into a larger target (self-sustaining
        // over-provisioning).
        state.recent_p50.push_back(p50);
        while state.recent_p50.len() > L_MIN_WINDOWS {
            state.recent_p50.pop_front();
        }
        state.l_min = state
            .recent_p50
            .iter()
            .copied()
            .fold(f64::INFINITY, f64::min)
            .max(0.01);
        let current = self.bound();
        // Remember the bound that produced the best rate so far; sustained
        // saturation stalls back to it.
        if rate >= state.t_best {
            state.t_best = rate;
            state.best_bound = current;
        } else {
            state.t_best *= T_BEST_DECAY;
        }
        state.windows += 1;

        let ramping = rate > previous * 1.05 && previous > 0.0;
        let governor_bound = self
            .rate_ceiling
            .is_some_and(|ceiling| rate >= 0.95 * ceiling);
        // Saturation is the only latency signal: every request slowing down
        // together past the ceiling. Workload-driven variance stays ignored.
        let overshot = p95 > LATENCY_CEILING_SECONDS || p50 > LATENCY_CEILING_SECONDS;

        let next = if governor_bound {
            state.mode = Mode::Hold;
            state.bad_windows = 0;
            // Little's law pins the operating point: in-flight just covers the
            // budgeted rate at the no-queue latency; pacing is the governor's.
            let ceiling = self.rate_ceiling.unwrap_or(rate);
            clamp_bound(1.1 * ceiling * state.l_min, self.ceiling)
        } else if overshot {
            state.mode = Mode::Backoff;
            state.bad_windows += 1;
            if state.bad_windows >= BACKOFF_STRIKES {
                // Sustained saturation: fall back to the last known knee.
                clamp_bound(state.best_bound as f64, self.ceiling)
            } else {
                clamp_bound(BACKOFF_GENTLE * current as f64, self.ceiling)
            }
        } else if ramping {
            state.bad_windows = 0;
            // Rising throughput is direct evidence there is headroom, so ramp
            // regardless of the previous mode. Flat windows never qualify.
            state.mode = Mode::Ramp;
            clamp_bound(RAMP_FACTOR * current as f64, self.ceiling)
        } else {
            state.bad_windows = 0;
            // Operating point: hold just above the bandwidth-delay product
            // (t_best x min-latency). The settle term is self-stabilising: a
            // bound that is too small lowers the achieved rate and therefore
            // the target, while a stalling bound trips the ceiling guard.
            let target = state.t_best * state.l_min;
            state.mode = Mode::Probe;
            clamp_bound(PROBE_FACTOR * target, self.ceiling)
        };
        self.bound.store(next, Ordering::Release);
        self.notify.notify_waiters();
    }
}

fn clamp_bound(target: f64, ceiling: usize) -> usize {
    if !target.is_finite() {
        return 2;
    }
    (target.round() as i64).clamp(2, ceiling as i64) as usize
}

fn percentile(samples: &[f64], fraction: f64) -> f64 {
    if samples.is_empty() {
        return 0.0;
    }
    let mut sorted = samples.to_vec();
    sorted.sort_by(|left, right| left.partial_cmp(right).unwrap_or(std::cmp::Ordering::Equal));
    let index = ((sorted.len() - 1) as f64 * fraction).round() as usize;
    sorted[index]
}

/// One controller per API key, process-wide.
pub fn lane_for(
    api_key: Option<&str>,
    requests_per_minute: Option<u32>,
    ceiling: usize,
) -> Arc<Lane> {
    static LANES: OnceLock<Mutex<HashMap<String, Arc<Lane>>>> = OnceLock::new();
    let lanes = LANES.get_or_init(|| Mutex::new(HashMap::new()));
    let key = api_key.unwrap_or("<anonymous>").to_string();
    let mut lanes = lanes
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    Arc::clone(
        lanes
            .entry(key)
            .or_insert_with(|| Arc::new(Lane::new(requests_per_minute, ceiling))),
    )
}

#[cfg(test)]
#[path = "../tests/unit/lane.rs"]
mod tests;
