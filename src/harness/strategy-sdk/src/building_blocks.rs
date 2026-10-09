//! Explicit numeric building blocks. Callers own symbol/timeframe state and
//! publish timing-bearing outputs; these functions never read market data.
use std::collections::VecDeque;

/// Trailing EMA, initialized from the first observation; warm-up remains caller-declared.
pub struct Ema {
    alpha: f64,
    value: Option<f64>,
}
impl Ema {
    /// Construct with a positive period.
    /// # Errors
    /// Rejects a zero period.
    pub fn new(period: usize) -> Result<Self, String> {
        if period == 0 {
            return Err("EMA period must be positive".into());
        }
        Ok(Self {
            alpha: 2.0 / (period as f64 + 1.0),
            value: None,
        })
    }
    /// Observe one finite value.
    /// # Errors
    /// Rejects nonfinite input.
    pub fn observe(&mut self, value: f64) -> Result<f64, String> {
        if !value.is_finite() {
            return Err("EMA value must be finite".into());
        }
        let result = self.value.map_or(value, |prior| {
            self.alpha * value + (1.0 - self.alpha) * prior
        });
        self.value = Some(result);
        Ok(result)
    }
}

/// Rolling simple-average RSI; this is explicitly not Wilder smoothing.
pub struct Rsi {
    period: usize,
    previous: Option<f64>,
    changes: VecDeque<f64>,
}
impl Rsi {
    /// Construct a positive-period RSI.
    /// # Errors
    /// Rejects a zero period.
    pub fn new(period: usize) -> Result<Self, String> {
        if period == 0 {
            return Err("RSI period must be positive".into());
        }
        Ok(Self {
            period,
            previous: None,
            changes: VecDeque::new(),
        })
    }
    /// Return undefined during warm-up or a flat window.
    /// # Errors
    /// Rejects nonfinite input.
    pub fn observe(&mut self, price: f64) -> Result<Option<f64>, String> {
        if !price.is_finite() {
            return Err("RSI price must be finite".into());
        }
        if let Some(previous) = self.previous {
            self.changes.push_back(price - previous);
        }
        self.previous = Some(price);
        if self.changes.len() > self.period {
            self.changes.pop_front();
        }
        if self.changes.len() < self.period {
            return Ok(None);
        }
        let gains = self.changes.iter().map(|x| x.max(0.0)).sum::<f64>();
        let losses = self.changes.iter().map(|x| (-x).max(0.0)).sum::<f64>();
        Ok(if gains + losses == 0.0 {
            None
        } else {
            Some(100.0 * gains / (gains + losses))
        })
    }
}

/// Rolling simple-average true range, using the previous observed close.
pub struct Atr {
    moments: RollingMoments,
    previous_close: Option<f64>,
}
impl Atr {
    /// Construct a positive-period ATR.
    /// # Errors
    /// Rejects a zero period.
    pub fn new(period: usize) -> Result<Self, String> {
        Ok(Self {
            moments: RollingMoments::new(period)?,
            previous_close: None,
        })
    }
    /// Observe one valid OHLC range; return undefined until full warm-up.
    /// # Errors
    /// Rejects nonfinite or inconsistent ranges without modifying state.
    pub fn observe(&mut self, high: f64, low: f64, close: f64) -> Result<Option<f64>, String> {
        if !high.is_finite()
            || !low.is_finite()
            || !close.is_finite()
            || high < low
            || close < low
            || close > high
        {
            return Err("ATR requires finite consistent OHLC".into());
        }
        let range = self.previous_close.map_or(high - low, |previous| {
            (high - low)
                .max((high - previous).abs())
                .max((low - previous).abs())
        });
        let value = self.moments.observe(range)?.map(|(mean, _)| mean);
        self.previous_close = Some(close);
        Ok(value)
    }
}

/// Rolling price-volume weighted mean; caller chooses close or typical price.
pub struct Vwap {
    period: usize,
    observations: VecDeque<(f64, f64)>,
}
impl Vwap {
    /// Construct a positive-period VWAP.
    /// # Errors
    /// Rejects zero length.
    pub fn new(period: usize) -> Result<Self, String> {
        if period == 0 {
            return Err("VWAP period must be positive".into());
        }
        Ok(Self {
            period,
            observations: VecDeque::new(),
        })
    }
    /// Return undefined until warm-up or when all window volume is zero.
    /// # Errors
    /// Rejects nonfinite values and negative volumes.
    pub fn observe(&mut self, price: f64, volume: f64) -> Result<Option<f64>, String> {
        if !price.is_finite() || !volume.is_finite() || volume < 0.0 {
            return Err("VWAP requires finite prices and nonnegative volumes".into());
        }
        self.observations.push_back((price, volume));
        if self.observations.len() > self.period {
            self.observations.pop_front();
        }
        let volume = self
            .observations
            .iter()
            .map(|(_, volume)| volume)
            .sum::<f64>();
        Ok(if self.observations.len() < self.period || volume == 0.0 {
            None
        } else {
            Some(
                self.observations
                    .iter()
                    .map(|(price, volume)| price * volume)
                    .sum::<f64>()
                    / volume,
            )
        })
    }
}

/// Track crossing direction and elapsed time in the current nonzero state.
#[derive(Default)]
pub struct CrossoverAge {
    direction: i8,
    changed_at: Option<i64>,
    last_time: Option<i64>,
}
impl CrossoverAge {
    /// Observe fast/slow values; equality retains the last nonzero state.
    /// # Errors
    /// Rejects nonfinite values and decreasing time.
    pub fn observe(
        &mut self,
        time_ns: i64,
        fast: f64,
        slow: f64,
    ) -> Result<(i8, Option<i64>, bool), String> {
        if !fast.is_finite() || !slow.is_finite() || self.last_time.is_some_and(|t| time_ns < t) {
            return Err("crossover requires finite time-ordered values".into());
        }
        self.last_time = Some(time_ns);
        let direction = if fast > slow {
            1
        } else if fast < slow {
            -1
        } else {
            self.direction
        };
        let crossed = self.direction != 0 && direction != self.direction;
        if direction != self.direction {
            self.changed_at = Some(time_ns);
            self.direction = direction;
        }
        Ok((direction, self.changed_at.map(|t| time_ns - t), crossed))
    }
}

/// Frozen deterministic normalization. Fit only on a declared development window.
#[derive(Clone, Debug, PartialEq)]
pub struct FittedNormalization {
    /// Training mean, never updated by inference.
    pub mean: f64,
    /// Training population standard deviation, never updated by inference.
    pub scale: f64,
}
impl FittedNormalization {
    /// Fit finite training observations; publish these weights with job/window provenance.
    /// # Errors
    /// Rejects fewer than two values, nonfinite data, and constant training data.
    pub fn fit(values: &[f64]) -> Result<Self, String> {
        if values.len() < 2 || values.iter().any(|x| !x.is_finite()) {
            return Err("normalization requires at least two finite training values".into());
        }
        let mean = values.iter().sum::<f64>() / values.len() as f64;
        let scale =
            (values.iter().map(|x| (x - mean).powi(2)).sum::<f64>() / values.len() as f64).sqrt();
        if !mean.is_finite() || !scale.is_finite() || scale == 0.0 {
            return Err("normalization requires finite nonzero training dispersion".into());
        }
        Ok(Self { mean, scale })
    }
    /// Apply frozen weights without fitting or mutating state.
    /// # Errors
    /// Rejects nonfinite input or invalid loaded weights.
    pub fn transform(&self, value: f64) -> Result<f64, String> {
        if !value.is_finite()
            || !self.mean.is_finite()
            || !self.scale.is_finite()
            || self.scale <= 0.0
        {
            return Err("normalization requires finite input and valid frozen weights".into());
        }
        Ok((value - self.mean) / self.scale)
    }
}

/// A confirmed directional change, including its earlier extreme.
#[derive(Clone, Debug, PartialEq)]
pub struct DirectionalEvent {
    /// +1 for upward confirmation, -1 for downward confirmation.
    pub direction: i8,
    /// Location of the turning extreme, unavailable as a confirmed turn then.
    pub extreme_time_ns: i64,
    /// Time at which the threshold move confirms the event.
    pub available_at_ns: i64,
    /// Extreme price.
    pub extreme: f64,
    /// Confirmation price.
    pub price: f64,
    /// Elapsed observed time since the previous confirmation.
    pub duration_ns: Option<i64>,
}

/// Stateful absolute-price-threshold directional change detector.
pub struct DirectionalChange {
    threshold: f64,
    direction: i8,
    high: Option<(i64, f64)>,
    low: Option<(i64, f64)>,
    last_time: Option<i64>,
    confirmation: Option<i64>,
}

impl DirectionalChange {
    /// Construct a detector with a fixed positive threshold.
    ///
    /// # Errors
    /// Rejects nonfinite or nonpositive thresholds.
    pub fn new(threshold: f64) -> Result<Self, String> {
        if !threshold.is_finite() || threshold <= 0.0 {
            return Err("directional change threshold must be positive and finite".into());
        }
        Ok(Self {
            threshold,
            direction: 0,
            high: None,
            low: None,
            last_time: None,
            confirmation: None,
        })
    }

    /// Observe one causal price. Never emits an event at an earlier extreme.
    ///
    /// # Errors
    /// Rejects nonfinite prices or decreasing observation timestamps.
    pub fn observe(
        &mut self,
        time_ns: i64,
        price: f64,
    ) -> Result<Option<DirectionalEvent>, String> {
        if !price.is_finite() || self.last_time.is_some_and(|last| time_ns < last) {
            return Err("directional change requires finite time-ordered prices".into());
        }
        self.last_time = Some(time_ns);
        if self.high.is_none_or(|(_, high)| price > high) {
            self.high = Some((time_ns, price));
        }
        if self.low.is_none_or(|(_, low)| price < low) {
            self.low = Some((time_ns, price));
        }
        let high = self.high.expect("observed high");
        let low = self.low.expect("observed low");
        let detected = if self.direction <= 0 && price - low.1 >= self.threshold {
            Some((1, low))
        } else if self.direction >= 0 && high.1 - price >= self.threshold {
            Some((-1, high))
        } else {
            None
        };
        Ok(detected.map(|(direction, (extreme_time_ns, extreme))| {
            let event = DirectionalEvent {
                direction,
                extreme_time_ns,
                available_at_ns: time_ns,
                extreme,
                price,
                duration_ns: self.confirmation.map(|last| time_ns - last),
            };
            self.direction = direction;
            self.confirmation = Some(time_ns);
            self.high = Some((time_ns, price));
            self.low = Some((time_ns, price));
            event
        }))
    }
}

/// Trailing mean and population dispersion for conditioning/compression.
pub struct RollingMoments {
    capacity: usize,
    values: VecDeque<f64>,
}

impl RollingMoments {
    /// Construct a strictly trailing window.
    ///
    /// # Errors
    /// Rejects zero length.
    pub fn new(capacity: usize) -> Result<Self, String> {
        if capacity == 0 {
            return Err("rolling window must be positive".into());
        }
        Ok(Self {
            capacity,
            values: VecDeque::new(),
        })
    }

    /// Add a value; return mean and standard deviation after full warm-up.
    ///
    /// # Errors
    /// Rejects nonfinite values without modifying state.
    pub fn observe(&mut self, value: f64) -> Result<Option<(f64, f64)>, String> {
        if !value.is_finite() {
            return Err("rolling value must be finite".into());
        }
        self.values.push_back(value);
        if self.values.len() > self.capacity {
            self.values.pop_front();
        }
        if self.values.len() < self.capacity {
            return Ok(None);
        }
        let mean = self.values.iter().sum::<f64>() / self.capacity as f64;
        let variance = self
            .values
            .iter()
            .map(|value| (value - mean).powi(2))
            .sum::<f64>()
            / self.capacity as f64;
        Ok(Some((mean, variance.sqrt())))
    }
}

/// Select perceptually important indices by vertical distance to each current
/// piecewise-linear segment. The whole result is available only at window end.
///
/// # Errors
/// Requires finite observations and 2..=window length points. Indices refer to
/// the supplied trailing window; they are never historical trading triggers.
pub fn perceptually_important_points(values: &[f64], count: usize) -> Result<Vec<usize>, String> {
    if count < 2 || count > values.len() || values.iter().any(|value| !value.is_finite()) {
        return Err("PIP requires finite values and 2..=window length points".into());
    }
    let mut selected = vec![0, values.len() - 1];
    while selected.len() < count {
        let mut best: Option<(usize, f64)> = None;
        for segment in selected.windows(2) {
            let (left, right) = (segment[0], segment[1]);
            for index in left + 1..right {
                let fraction = (index - left) as f64 / (right - left) as f64;
                let reference = values[left] + fraction * (values[right] - values[left]);
                let distance = (values[index] - reference).abs();
                if best.is_none_or(|(_, prior)| distance > prior) {
                    best = Some((index, distance));
                }
            }
        }
        let index = best.expect("unselected point exists").0;
        selected.push(index);
        selected.sort_unstable();
    }
    Ok(selected)
}

/// One orthonormal Haar level, from fine to coarse scale.
#[derive(Clone, Debug, PartialEq)]
pub struct HaarLevel {
    /// Number of source observations contributing to each coefficient.
    pub support: usize,
    /// Detail coefficients in source order.
    pub details: Vec<f64>,
}

/// Haar decomposition of a declared trailing window without wraparound or
/// future padding. All returned coefficients become available at window end.
///
/// # Errors
/// Requires a power-of-two window of at least two finite observations.
pub fn trailing_haar(values: &[f64]) -> Result<Vec<HaarLevel>, String> {
    if values.len() < 2
        || !values.len().is_power_of_two()
        || values.iter().any(|value| !value.is_finite())
    {
        return Err("Haar requires a finite power-of-two trailing window".into());
    }
    let mut current = values.to_vec();
    let mut levels = Vec::new();
    let mut support = 2;
    while current.len() > 1 {
        let mut averages = Vec::with_capacity(current.len() / 2);
        let mut details = Vec::with_capacity(current.len() / 2);
        for pair in current.as_chunks::<2>().0 {
            averages.push((pair[0] + pair[1]) / std::f64::consts::SQRT_2);
            details.push((pair[0] - pair[1]) / std::f64::consts::SQRT_2);
        }
        levels.push(HaarLevel { support, details });
        current = averages;
        support *= 2;
    }
    Ok(levels)
}
