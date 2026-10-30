//! Explicit numeric building blocks. Callers own symbol/timeframe state and
//! publish timing-bearing outputs; these functions never read market data.
use std::collections::VecDeque;

use crate::Decimal;
use crate::Fill;

/// Exact, single-instrument fill accounting. Every partial fill changes inventory.
/// This is an observation ledger, not an order manager: submission is not a fill,
/// and a first exit fill is not evidence that the position is flat. Fees may be
/// negative (rebates). Keep one ledger per instrument, including across sessions.
/// Cash flows use price * quantity; apply an explicit contract multiplier outside
/// this helper for instruments whose fill prices require one.
pub struct FillLedger {
    symbol: String,
    quantity: Decimal,
    cash: Decimal,
    fees: Decimal,
    fills: std::collections::BTreeMap<String, (String, String, Decimal, Decimal, Decimal)>,
    orders: std::collections::BTreeMap<String, (Decimal, Decimal)>,
}

impl FillLedger {
    /// Start an empty ledger for an exact instrument identity.
    pub fn new(symbol: &str) -> Self {
        Self {
            symbol: symbol.into(),
            quantity: Decimal::ZERO,
            cash: Decimal::ZERO,
            fees: Decimal::ZERO,
            fills: std::collections::BTreeMap::new(),
            orders: std::collections::BTreeMap::new(),
        }
    }

    /// Apply one fill exactly once; return false for an identical redelivery.
    /// # Errors
    /// Reject invalid fills and conflicting identities without changing state.
    pub fn apply(&mut self, fill: &Fill<'_>) -> Result<bool, String> {
        if fill.symbol != self.symbol
            || fill.fill_id.is_empty()
            || !matches!(fill.side, "buy" | "sell")
            || fill.quantity <= Decimal::ZERO
            || fill.price <= Decimal::ZERO
        {
            return Err(
                format!("FillLedger::apply fill_id={:?}, order_id={:?}, symbol={:?} (expected {:?}), side={:?}, quantity={}, price={}: require identity, matching symbol, buy/sell, positive quantity and price", fill.fill_id, fill.order_id, fill.symbol, self.symbol, fill.side, fill.quantity, fill.price),
            );
        }
        let record = (
            fill.order_id.into(),
            fill.side.into(),
            fill.quantity,
            fill.price,
            fill.fee,
        );
        if let Some(previous) = self.fills.get(fill.fill_id) {
            return if previous == &record {
                Ok(false)
            } else {
                Err(format!("FillLedger fill_id={:?}: conflicting fill identity; previous={previous:?}, received={record:?}", fill.fill_id))
            };
        }
        // Prepare every checked change before committing any state. An invalid
        // fill must not leave partially updated inventory or cash behind.
        let signed = fill.signed_quantity();
        let checked = |error: num::DecimalError| format!("fill accounting arithmetic: {error}");
        let quantity = self.quantity.checked_add(signed).map_err(checked)?;
        let notional = fill.quantity.checked_mul(fill.price).map_err(checked)?;
        let cash = if fill.side == "buy" {
            self.cash.checked_sub(notional)
        } else {
            self.cash.checked_add(notional)
        }
        .map_err(checked)?;
        let fees = self.fees.checked_add(fill.fee).map_err(checked)?;
        let previous = self
            .orders
            .get(fill.order_id)
            .copied()
            .unwrap_or((Decimal::ZERO, Decimal::ZERO));
        let order_quantity = previous.0.checked_add(fill.quantity).map_err(checked)?;
        let order_notional = previous.1.checked_add(notional).map_err(checked)?;
        self.quantity = quantity;
        self.cash = cash;
        self.fees = fees;
        self.orders
            .insert(fill.order_id.into(), (order_quantity, order_notional));
        self.fills.insert(fill.fill_id.into(), record);
        Ok(true)
    }

    /// Actual signed filled inventory; zero alone establishes flatness.
    pub fn quantity(&self) -> Decimal {
        self.quantity
    }
    /// Signed cash from all fills, before fees. This is not realized P&L while open.
    pub fn cash_flow(&self) -> Decimal {
        self.cash
    }
    /// Accumulated actual fees, including rebates.
    pub fn fees(&self) -> Decimal {
        self.fees
    }
    /// Total filled quantity for an order, across all its partial fills.
    pub fn filled_quantity(&self, order_id: &str) -> Decimal {
        self.orders
            .get(order_id)
            .map_or(Decimal::ZERO, |record| record.0)
    }
    /// Quantity-weighted price over every partial fill of this order.
    pub fn average_fill_price(&self, order_id: &str) -> Option<Decimal> {
        self.orders
            .get(order_id)
            .and_then(|record| record.1.checked_div(record.0).ok())
    }
    /// Marked P&L from fills, inventory and fees; not a return or a closed-trade metric.
    /// # Errors
    /// Reject a nonpositive mark.
    pub fn net_pnl(&self, mark: Decimal) -> Result<Decimal, String> {
        if mark <= Decimal::ZERO {
            return Err(format!(
                "FillLedger::net_pnl mark={mark}: expected a positive price"
            ));
        }
        self.quantity
            .checked_mul(mark)
            .and_then(|value| self.cash.checked_add(value))
            .and_then(|value| value.checked_sub(self.fees))
            .map_err(|error| format!("fill accounting arithmetic: {error}"))
    }
}

/// Requested quantity for one accepted order. This does not submit, cancel or
/// infer an order's terminal state. Keep it while fills remain outstanding;
/// explicit rejection/cancellation evidence is a separate caller-owned outcome.
pub struct PendingOrder {
    order_id: String,
    quantity: Decimal,
}

impl PendingOrder {
    /// Capture an accepted submission, never a guessed first-fill quantity.
    pub fn new(order_id: &str, quantity: Decimal) -> Result<Self, String> {
        if order_id.is_empty() || quantity <= Decimal::ZERO {
            return Err(format!("PendingOrder::new order_id={order_id:?}, quantity={quantity}: require a nonempty ID and positive requested quantity"));
        }
        Ok(Self {
            order_id: order_id.into(),
            quantity,
        })
    }
    pub fn order_id(&self) -> &str {
        &self.order_id
    }
    /// Remaining order quantity, using every acknowledged fill exactly once.
    pub fn remaining(&self, ledger: &FillLedger) -> Result<Decimal, String> {
        let remaining = self
            .quantity
            .checked_sub(ledger.filled_quantity(&self.order_id))
            .map_err(|error| error.to_string())?;
        if remaining < Decimal::ZERO {
            return Err(format!(
                "PendingOrder {:?}: filled quantity {} exceeds requested {}",
                self.order_id,
                ledger.filled_quantity(&self.order_id),
                self.quantity
            ));
        }
        Ok(remaining)
    }
    /// False also covers an inconsistent ledger. Use `remaining` when the
    /// caller needs to distinguish outstanding fills from an accounting error.
    pub fn is_filled(&self, ledger: &FillLedger) -> bool {
        self.remaining(ledger)
            .is_ok_and(|quantity| quantity.is_zero())
    }
}

/// Convert cash P&L to basis points of positive entry notional, never price alone.
/// Callers provide aggregate filled entry quantity and its quantity-weighted price.
/// # Errors
/// Reject zero quantity and nonpositive price; use an explicit multiplier for contracts.
pub fn cash_return_bps(
    pnl: Decimal,
    entry_quantity: Decimal,
    entry_price: Decimal,
) -> Result<f64, String> {
    if entry_quantity == Decimal::ZERO || entry_price <= Decimal::ZERO {
        return Err(format!("cash_return_bps entry_quantity={entry_quantity}, entry_price={entry_price}: require nonzero filled quantity and positive entry price"));
    }
    let notional = entry_quantity
        .abs()
        .checked_mul(entry_price)
        .map_err(|error| error.to_string())?;
    pnl.checked_div(notional)
        .and_then(|value| value.checked_mul(Decimal::from_i128(10_000)))
        .and_then(Decimal::to_f64_checked)
        .map_err(|error| error.to_string())
}

/// Nearest-rank empirical quantile: rank ceil(q*n), clamped to 1..n.
/// Supply only observations available at the decision; fitting/freeze policy is
/// caller-owned. A quantile rank is not an absolute feature threshold.
/// # Errors
/// Reject empty/nonfinite samples or a probability outside [0,1].
pub fn empirical_quantile(values: &[f64], q: f64) -> Result<f64, String> {
    if values.is_empty()
        || values.iter().any(|x| !x.is_finite())
        || !q.is_finite()
        || !(0.0..=1.0).contains(&q)
    {
        return Err(format!("empirical_quantile sample_count={}, first_nonfinite={:?}, q={q:?}: require nonempty finite observations and probability in [0,1]", values.len(), values.iter().position(|v| !v.is_finite())));
    }
    let mut sorted = values.to_vec();
    sorted.sort_by(f64::total_cmp);
    let rank = (q * sorted.len() as f64).ceil().max(1.0) as usize;
    Ok(sorted[rank - 1])
}

/// OLS slope of log prices against equally spaced bar indices, in bps per bar.
/// This uses every observation, not just the endpoints. Callers own availability.
/// # Errors
/// Reject fewer than two finite positive prices.
pub fn log_price_slope_bps(prices: &[f64]) -> Result<f64, String> {
    if prices.len() < 2 || prices.iter().any(|p| !p.is_finite() || *p <= 0.0) {
        return Err(format!("log_price_slope_bps sample_count={}, first_invalid={:?}: require at least two finite positive prices", prices.len(), prices.iter().position(|v| !v.is_finite() || *v <= 0.0)));
    }
    let center = (prices.len() - 1) as f64 / 2.0;
    let mut numerator = 0.0;
    let mut denominator = 0.0;
    for (i, price) in prices.iter().enumerate() {
        let x = i as f64 - center;
        numerator += x * price.ln();
        denominator += x * x;
    }
    let result = 10_000.0 * numerator / denominator;
    if !result.is_finite() {
        return Err(
            "log_price_slope_bps: arithmetic produced a nonfinite slope; rescale the observations"
                .into(),
        );
    }
    Ok(result)
}

/// Ratio of expected orthonormal Haar detail energies for an iid-increment
/// random walk: E_s=(window/s)*(s*s+2)/12, for supports s=2,4,...,window.
/// Sum supports >= coarse_support in the numerator. This is a ratio of expected
/// energies, not the expected sample ratio and not an estimated market regime.
/// # Errors
/// Require power-of-two window/support with 2 <= support <= window.
pub fn haar_random_walk_energy_share(window: usize, coarse_support: usize) -> Result<f64, String> {
    if !window.is_power_of_two()
        || !coarse_support.is_power_of_two()
        || coarse_support < 2
        || coarse_support > window
    {
        return Err(
            format!("haar_random_walk_energy_share window={window}, coarse_support={coarse_support}: require powers of two with 2 <= support <= window"),
        );
    }
    let mut total = 0.0;
    let mut coarse = 0.0;
    let mut support = 2;
    loop {
        let s = support as f64;
        let energy = (window as f64 / s) * (s * s + 2.0) / 12.0;
        total += energy;
        if support >= coarse_support {
            coarse += energy;
        }
        if support == window {
            break;
        }
        support *= 2;
    }
    Ok(coarse / total)
}

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
            return Err(format!(
                "Ema::new period={period}: expected a positive observation count"
            ));
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
            return Err(format!(
                "Ema::observe value={value:?}: expected a finite value"
            ));
        }
        let result = self.value.map_or(value, |prior| {
            self.alpha * value + (1.0 - self.alpha) * prior
        });
        if !result.is_finite() {
            return Err(format!(
                "Ema::observe value={value:?}: arithmetic produced a nonfinite result"
            ));
        }
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
            return Err(format!(
                "Rsi::new period={period}: expected a positive change count"
            ));
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
            return Err(format!(
                "Rsi::observe price={price:?}: expected a finite value"
            ));
        }
        if let Some(previous) = self.previous {
            if !(price - previous).is_finite() {
                return Err(format!(
                    "Rsi::observe price={price:?}, previous={previous:?}: nonfinite change"
                ));
            }
            self.changes.push_back(price - previous);
        }
        self.previous = Some(price);
        if self.changes.len() > self.period {
            self.changes.pop_front();
        }
        if self.changes.len() < self.period {
            return Ok(None);
        }
        let largest = self.changes.iter().map(|x| x.abs()).fold(0.0_f64, f64::max);
        if largest == 0.0 {
            return Ok(None);
        }
        let gains = self
            .changes
            .iter()
            .map(|x| (x / largest).max(0.0))
            .sum::<f64>();
        let losses = self
            .changes
            .iter()
            .map(|x| (-x / largest).max(0.0))
            .sum::<f64>();
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
            return Err(format!("Atr::observe high={high:?}, low={low:?}, close={close:?}: require finite values and low <= close <= high"));
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
            return Err(format!(
                "Vwap::new period={period}: expected a positive observation count"
            ));
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
            return Err(format!("Vwap::observe price={price:?}, volume={volume:?}: require finite prices and finite nonnegative volume"));
        }
        let mut observations = self.observations.clone();
        observations.push_back((price, volume));
        if observations.len() > self.period {
            observations.pop_front();
        }
        let volume_sum = observations.iter().map(|(_, v)| v).sum::<f64>();
        let notional = observations.iter().map(|(p, v)| p * v).sum::<f64>();
        if !volume_sum.is_finite() || !notional.is_finite() {
            return Err(format!("Vwap::observe price={price:?}, volume={volume:?}: rolling volume/notional overflow; rescale the inputs"));
        }
        let result = if observations.len() < self.period || volume_sum == 0.0 {
            None
        } else {
            Some(notional / volume_sum)
        };
        if result.is_some_and(|v| !v.is_finite()) {
            return Err("Vwap::observe: nonfinite weighted mean; rescale the inputs".into());
        }
        self.observations = observations;
        Ok(result)
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
            return Err(format!("CrossoverAge::observe time_ns={time_ns}, previous_time={:?}, fast={fast:?}, slow={slow:?}: require finite values and nondecreasing nanosecond timestamps", self.last_time));
        }
        let direction = if fast > slow {
            1
        } else if fast < slow {
            -1
        } else {
            self.direction
        };
        let age = if direction != self.direction {
            Some(0)
        } else {
            self.changed_at
                .map(|t| {
                    time_ns.checked_sub(t).ok_or_else(|| {
                        format!("CrossoverAge duration overflow: {time_ns} - {t} nanoseconds")
                    })
                })
                .transpose()?
        };
        self.last_time = Some(time_ns);
        let crossed = self.direction != 0 && direction != self.direction;
        if direction != self.direction {
            self.changed_at = Some(time_ns);
            self.direction = direction;
        }
        Ok((direction, age, crossed))
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
            return Err(format!("FittedNormalization::fit sample_count={}, first_nonfinite={:?}: require at least two finite training values", values.len(), values.iter().position(|v| !v.is_finite())));
        }
        let mean = values.iter().sum::<f64>() / values.len() as f64;
        let scale =
            (values.iter().map(|x| (x - mean).powi(2)).sum::<f64>() / values.len() as f64).sqrt();
        if !mean.is_finite() || !scale.is_finite() || scale == 0.0 {
            return Err(format!("FittedNormalization::fit mean={mean:?}, scale={scale:?}: require finite mean and finite nonzero dispersion"));
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
            return Err(format!("FittedNormalization::transform value={value:?}, mean={:?}, scale={:?}: require finite input/mean and positive finite scale", self.mean, self.scale));
        }
        let result = (value - self.mean) / self.scale;
        if !result.is_finite() {
            return Err(format!("FittedNormalization::transform value={value:?}, mean={:?}, scale={:?}: arithmetic produced a nonfinite result", self.mean, self.scale));
        }
        Ok(result)
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
            return Err(format!("DirectionalChange::new threshold={threshold:?}: expected a positive finite absolute price distance"));
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
            return Err(format!("DirectionalChange::observe time_ns={time_ns}, previous_time={:?}, price={price:?}: require finite prices and nondecreasing nanosecond timestamps", self.last_time));
        }
        let duration = self
            .confirmation
            .map(|last| {
                time_ns.checked_sub(last).ok_or_else(|| {
                    format!("DirectionalChange duration overflow: {time_ns} - {last} nanoseconds")
                })
            })
            .transpose()?;
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
                duration_ns: duration,
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
            return Err(format!(
                "RollingMoments::new capacity={capacity}: expected a positive observation count"
            ));
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
            return Err(format!(
                "RollingMoments::observe value={value:?}: expected a finite value"
            ));
        }
        self.values.push_back(value);
        let expired = if self.values.len() > self.capacity {
            self.values.pop_front()
        } else {
            None
        };
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
        if !mean.is_finite() || !variance.is_finite() {
            self.values.pop_back();
            if let Some(previous) = expired {
                self.values.push_front(previous);
            }
            return Err(format!("RollingMoments::observe value={value:?}, capacity={}: arithmetic produced nonfinite moments; rescale the inputs", self.capacity));
        }
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
        return Err(format!("perceptually_important_points count={count}, sample_count={}, first_nonfinite={:?}: require finite values and 2 <= count <= sample_count", values.len(), values.iter().position(|v| !v.is_finite())));
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
                if !distance.is_finite() {
                    return Err(format!("perceptually_important_points values[{index}]: nonfinite interpolated distance; rescale the inputs"));
                }
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
pub(crate) struct HaarLevel {
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
pub(crate) fn trailing_haar(values: &[f64]) -> Result<Vec<HaarLevel>, String> {
    if values.len() < 2
        || !values.len().is_power_of_two()
        || values.iter().any(|value| !value.is_finite())
    {
        return Err(format!("trailing_haar sample_count={}, first_nonfinite={:?}: require a power-of-two window of at least two finite observations", values.len(), values.iter().position(|v| !v.is_finite())));
    }
    let mut current = values.to_vec();
    let mut levels = Vec::new();
    let mut support = 2;
    while current.len() > 1 {
        let mut averages = Vec::with_capacity(current.len() / 2);
        let mut details = Vec::with_capacity(current.len() / 2);
        for pair in current.as_chunks::<2>().0 {
            let average = (pair[0] + pair[1]) / std::f64::consts::SQRT_2;
            let detail = (pair[0] - pair[1]) / std::f64::consts::SQRT_2;
            if !average.is_finite() || !detail.is_finite() {
                return Err(format!("trailing_haar support={support}: nonfinite coefficient from {:?}; rescale the inputs", pair));
            }
            averages.push(average);
            details.push(detail);
        }
        levels.push(HaarLevel { support, details });
        current = averages;
        support *= 2;
    }
    Ok(levels)
}
