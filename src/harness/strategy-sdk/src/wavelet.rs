//! Managed trailing Haar features and immutable decision-time observations.
//!
//! Own this indicator in a MarketConsumer and return it from indicators().
//! Routing updates it from completed source bars before the owner's callback.
//! Call snapshot_at(symbol, frame.available_at_ns) at the decision and retain the
//! result. Historical lookup never substitutes a later value. Warm-up and missing
//! input return None rather than carrying a stale feature across missing bars.
use std::collections::BTreeMap;
use std::collections::VecDeque;

use crate::canonical_subscriptions;
use crate::indicator_add;
use crate::indicator_div;
use crate::indicator_mul;
use crate::indicator_sub;
use crate::BarFrame;
use crate::Decimal;
use crate::Indicator;
use crate::IndicatorSequence;
use crate::MarketConsumer;
use crate::ObservationContext;
use crate::Subscription;

/// One scale's detail energy, with support measured in source bars.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WaveletScale {
    support: usize,
    energy: Decimal,
}
impl WaveletScale {
    /// Number of bars contributing to a coefficient at this scale.
    pub fn support(&self) -> usize {
        self.support
    }
    /// Sum of orthonormal detail coefficient squares at this scale.
    pub fn energy(&self) -> Decimal {
        self.energy
    }
}

/// Immutable wavelet features selected at a particular decision time.
/// Fields are private: callers cannot alter availability or relabel a snapshot.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WaveletSnapshot {
    symbol: String,
    decision_time_ns: i64,
    available_at_ns: i64,
    window_start_ns: i64,
    coarse_energy_share: Decimal,
    trend_bps: Decimal,
    scales: Vec<WaveletScale>,
}
impl WaveletSnapshot {
    /// Source symbol.
    pub fn symbol(&self) -> &str {
        &self.symbol
    }
    /// The as-of decision bound used to select this immutable observation.
    pub fn decision_time_ns(&self) -> i64 {
        self.decision_time_ns
    }
    /// When all inputs to these features became observable.
    pub fn available_at_ns(&self) -> i64 {
        self.available_at_ns
    }
    /// Start of the trailing source window.
    pub fn window_start_ns(&self) -> i64 {
        self.window_start_ns
    }
    /// Fraction of detail energy in configured coarse scales. Flat windows are undefined.
    pub fn coarse_energy_share(&self) -> Decimal {
        self.coarse_energy_share
    }
    /// Difference of latest-half and earlier-half means, divided by the whole-window
    /// mean, in basis points. This supplies sign; energy itself is unsigned.
    pub fn trend_bps(&self) -> Decimal {
        self.trend_bps
    }
    /// Fine-to-coarse orthonormal detail energies.
    pub fn scales(&self) -> &[WaveletScale] {
        &self.scales
    }
}

#[derive(Clone)]
struct Observation {
    available_at_ns: i64,
    snapshot: Option<WaveletSnapshot>,
}

/// SDK-owned wavelet computation. No centered filters, padding or future inputs.
/// The window and coarse support are powers of two; state is isolated by symbol.
pub struct WaveletIndicator {
    symbols: Vec<String>,
    interval: String,
    window: usize,
    coarse_support: usize,
    inputs: BTreeMap<String, VecDeque<(i64, Decimal)>>,
    observations: BTreeMap<String, Vec<Observation>>,
    sequence: IndicatorSequence,
}
impl WaveletIndicator {
    /// Declare exact completed-bar inputs and the trailing feature parameters.
    /// Missing source bars reset warm-up; session/calendar gaps are not fabricated.
    pub fn new(
        symbols: Vec<String>,
        interval: String,
        window: usize,
        coarse_support: usize,
    ) -> Result<Self, String> {
        if !(2..=4096).contains(&window)
            || !window.is_power_of_two()
            || coarse_support < 2
            || coarse_support > window
            || !coarse_support.is_power_of_two()
        {
            return Err("wavelet window must be a power of two in 2..4096; coarse support must be a power of two in 2..window".into());
        }
        let subscriptions =
            canonical_subscriptions(vec![Subscription::Bars { symbols, interval }])?;
        let Subscription::Bars { symbols, interval } =
            subscriptions.into_iter().next().expect("one subscription")
        else {
            unreachable!()
        };
        Ok(Self {
            symbols,
            interval,
            window,
            coarse_support,
            inputs: BTreeMap::new(),
            observations: BTreeMap::new(),
            sequence: IndicatorSequence::default(),
        })
    }

    /// Read features observable at the ORIGINAL decision time, even when called
    /// after later bars. A missing/undefined latest observation yields None.
    /// Keep this snapshot with the setup instead of reading Indicator::value later.
    pub fn snapshot_at(&self, symbol: &str, decision_time_ns: i64) -> Option<WaveletSnapshot> {
        let history = self.observations.get(symbol)?;
        let end = history.partition_point(|item| item.available_at_ns <= decision_time_ns);
        let mut snapshot = history.get(end.checked_sub(1)?)?.snapshot.clone()?;
        snapshot.decision_time_ns = decision_time_ns;
        Some(snapshot)
    }

    fn compute(
        symbol: &str,
        available_at_ns: i64,
        values: &VecDeque<(i64, Decimal)>,
        coarse_support: usize,
    ) -> Option<WaveletSnapshot> {
        let two = Decimal::from(2);
        let mut current: Vec<_> = values.iter().map(|(_, close)| *close).collect();
        let mut support = 2;
        let mut weight = Decimal::from(2);
        let mut total = Decimal::ZERO;
        let mut coarse = Decimal::ZERO;
        let mut scales = Vec::new();
        let mut last_difference = Decimal::ZERO;
        while current.len() > 1 {
            let mut averages = Vec::with_capacity(current.len() / 2);
            let mut energy = Decimal::ZERO;
            for pair in current.as_chunks::<2>().0 {
                let average = indicator_div(indicator_add(pair[0], pair[1]).ok()?, two).ok()?;
                let detail = indicator_div(indicator_sub(pair[0], pair[1]).ok()?, two).ok()?;
                energy = indicator_add(
                    energy,
                    indicator_mul(indicator_mul(detail, detail).ok()?, weight).ok()?,
                )
                .ok()?;
                averages.push(average);
                last_difference = indicator_sub(pair[1], pair[0]).ok()?;
            }
            total = indicator_add(total, energy).ok()?;
            if support >= coarse_support {
                coarse = indicator_add(coarse, energy).ok()?;
            }
            scales.push(WaveletScale { support, energy });
            support *= 2;
            weight = indicator_mul(weight, two).ok()?;
            current = averages;
        }
        if total == Decimal::ZERO || current[0] == Decimal::ZERO {
            return None;
        }
        Some(WaveletSnapshot {
            symbol: symbol.into(),
            decision_time_ns: available_at_ns,
            available_at_ns,
            window_start_ns: values.front()?.0,
            coarse_energy_share: indicator_div(coarse, total).ok()?,
            trend_bps: indicator_mul(
                indicator_div(last_difference, current[0]).ok()?,
                Decimal::from(10000),
            )
            .ok()?,
            scales,
        })
    }
}
impl MarketConsumer for WaveletIndicator {
    type Context = ObservationContext;
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription::Bars {
            symbols: self.symbols.clone(),
            interval: self.interval.clone(),
        }]
    }
    fn on_bar(&mut self, frame: &BarFrame, _: &mut ObservationContext) {
        for symbol in &self.symbols {
            let inputs = self.inputs.entry(symbol.clone()).or_default();
            let snapshot = if let Some(bar) = frame.bars_by_symbol.get(symbol.as_str()) {
                inputs.push_back((frame.interval_start_ns, bar.close));
                if inputs.len() > self.window {
                    inputs.pop_front();
                }
                if inputs.len() == self.window {
                    Self::compute(symbol, frame.available_at_ns, inputs, self.coarse_support)
                } else {
                    None
                }
            } else {
                inputs.clear();
                None
            };
            self.observations
                .entry(symbol.clone())
                .or_default()
                .push(Observation {
                    available_at_ns: frame.available_at_ns,
                    snapshot,
                });
        }
    }
}
impl Indicator for WaveletIndicator {
    fn value(&self, symbol: &str) -> Option<Decimal> {
        self.observations
            .get(symbol)?
            .last()?
            .snapshot
            .as_ref()
            .map(WaveletSnapshot::coarse_energy_share)
    }
    fn sequence(&self) -> &IndicatorSequence {
        &self.sequence
    }
    fn sequence_mut(&mut self) -> &mut IndicatorSequence {
        &mut self.sequence
    }
}
