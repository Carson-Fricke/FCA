//! Causal fill accounting and compact valuation history.
//!
//! The replay updates cash and inventory once, using only marks already observed
//! at that dispatch. Consecutive identical financial states retain their logical
//! sample count and last observed time. Reporting keeps minute-bucket endpoints,
//! extrema and every fill boundary; metrics still consume the full sample stream.

use std::collections::BTreeMap;

use num::Decimal;
use num::Decimal18;

use super::history::take;
use super::history::History;
use super::history::Record;
use crate::results::metrics::EquityAccumulator;
use crate::results::metrics::EquityMetrics;
use crate::results::metrics::EquityPoint;
use harness_trading::program::EngineFill;
use harness_trading::replay_errors::BacktestError;

/// Reporting buckets are epoch-aligned; no unobserved timestamp is synthesized.
pub const REPORT_INTERVAL_NS: i64 = 60_000_000_000;

/// One financial state, possibly repeated across consecutive dispatches.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ValuationRow {
    pub timestamp_ns: i64,
    pub end_timestamp_ns: i64,
    pub sample_index: u64,
    pub samples: u64,
    pub cash: Decimal18,
    pub equity: Decimal18,
    pub market_value: Decimal18,
    pub position_count: u32,
    pub fill_count: u64,
}

impl ValuationRow {
    fn same_state(&self, other: &Self) -> bool {
        self.cash == other.cash
            && self.equity == other.equity
            && self.market_value == other.market_value
            && self.position_count == other.position_count
            && self.fill_count == other.fill_count
    }

    fn endpoint(&self) -> Self {
        Self {
            timestamp_ns: self.end_timestamp_ns,
            sample_index: self.sample_index + self.samples - 1,
            samples: 1,
            ..self.clone()
        }
    }

    fn first(&self) -> Self {
        Self {
            end_timestamp_ns: self.timestamp_ns,
            samples: 1,
            ..self.clone()
        }
    }

    pub fn point(&self) -> EquityPoint {
        EquityPoint {
            timestamp_ns: self.timestamp_ns,
            equity: self.equity,
        }
    }
}

impl Record for ValuationRow {
    fn encode(&self, output: &mut Vec<u8>) {
        output.extend(self.timestamp_ns.to_le_bytes());
        output.extend(self.end_timestamp_ns.to_le_bytes());
        output.extend(self.sample_index.to_le_bytes());
        output.extend(self.samples.to_le_bytes());
        for value in [self.cash, self.equity, self.market_value] {
            output.extend(value.unscaled().to_le_bytes());
        }
        output.extend(self.position_count.to_le_bytes());
        output.extend(self.fill_count.to_le_bytes());
    }

    fn decode(input: &mut &[u8]) -> std::io::Result<Self> {
        Ok(Self {
            timestamp_ns: i64::from_le_bytes(take(input)?),
            end_timestamp_ns: i64::from_le_bytes(take(input)?),
            sample_index: u64::from_le_bytes(take(input)?),
            samples: u64::from_le_bytes(take(input)?),
            cash: Decimal18::from_unscaled(i128::from_le_bytes(take(input)?))
                .map_err(std::io::Error::other)?,
            equity: Decimal18::from_unscaled(i128::from_le_bytes(take(input)?))
                .map_err(std::io::Error::other)?,
            market_value: Decimal18::from_unscaled(i128::from_le_bytes(take(input)?))
                .map_err(std::io::Error::other)?,
            position_count: u32::from_le_bytes(take(input)?),
            fill_count: u64::from_le_bytes(take(input)?),
        })
    }
}

#[derive(Clone, Debug)]
struct Holding {
    quantity: Decimal18,
    mark: Option<Decimal18>,
    value: Decimal18,
}

impl Default for Holding {
    fn default() -> Self {
        Self {
            quantity: Decimal18::ZERO,
            mark: None,
            value: Decimal18::ZERO,
        }
    }
}

fn arithmetic(error: num::DecimalError) -> BacktestError {
    BacktestError::Arithmetic(format!("causal valuation arithmetic: {error}"))
}

impl Holding {
    fn revalue(&mut self) -> Result<(), BacktestError> {
        self.value = if self.quantity.is_zero() {
            Decimal18::ZERO
        } else {
            self.quantity
                .checked_mul(self.mark.ok_or_else(|| {
                    BacktestError::Arithmetic("open position has no observed valuation mark".into())
                })?)
                .map_err(arithmetic)?
        };
        Ok(())
    }
}

/// Mutable replay-owned accounting. Never formats or reconstructs a fill ledger.
pub struct ValuationTracker {
    cash: Decimal18,
    holdings: BTreeMap<String, Holding>,
    current: ValuationRow,
    rows: History<ValuationRow>,
    samples: u64,
    consumed_fills: usize,
    peak_gross_exposure: Decimal18,
}

impl ValuationTracker {
    pub fn new(
        timestamp_ns: i64,
        initial_cash: Decimal,
        resources: super::history::HistoryResources,
    ) -> Result<Self, BacktestError> {
        let cash = Decimal18::from_decimal(initial_cash).map_err(arithmetic)?;
        Ok(Self {
            cash,
            holdings: BTreeMap::new(),
            current: ValuationRow {
                timestamp_ns,
                end_timestamp_ns: timestamp_ns,
                sample_index: 0,
                samples: 1,
                cash,
                equity: cash,
                market_value: Decimal18::ZERO,
                position_count: 0,
                fill_count: 0,
            },
            rows: History::new(resources),
            samples: 1,
            consumed_fills: 0,
            peak_gross_exposure: Decimal18::ZERO,
        })
    }

    /// Observe one market/frame/fill dispatch after its booked fills and marks.
    pub fn observe<'a>(
        &mut self,
        timestamp_ns: i64,
        marks: impl IntoIterator<Item = (&'a str, Decimal)>,
        fills: &[EngineFill],
    ) -> Result<(), BacktestError> {
        if timestamp_ns < self.current.end_timestamp_ns || fills.len() < self.consumed_fills {
            return Err(BacktestError::Arithmetic(
                "valuation regressed causal order".into(),
            ));
        }
        let mut dirty = false;
        for fill in &fills[self.consumed_fills..] {
            if fill.timestamp_ns > timestamp_ns || !matches!(fill.side.as_str(), "buy" | "sell") {
                return Err(BacktestError::Arithmetic("invalid causal fill".into()));
            }
            let quantity = Decimal18::from_decimal(fill.quantity).map_err(arithmetic)?;
            let price = Decimal18::from_decimal(fill.price).map_err(arithmetic)?;
            let fee = Decimal18::from_decimal(fill.fee).map_err(arithmetic)?;
            let notional =
                quantity
                    .checked_mul(price)
                    .map_err(|_| BacktestError::MalformedFill {
                        fill_id: fill.fill_id.clone(),
                        detail:
                            "notional (quantity times price) overflows the decimal128(38,18) bound"
                                .into(),
                    })?;
            let holding = self.holdings.entry(fill.symbol.clone()).or_default();
            if fill.side == "buy" {
                self.cash = self
                    .cash
                    .checked_sub(notional)
                    .and_then(|v| v.checked_sub(fee))
                    .map_err(arithmetic)?;
                holding.quantity = holding.quantity.checked_add(quantity).map_err(arithmetic)?;
            } else {
                self.cash = self
                    .cash
                    .checked_add(notional)
                    .and_then(|v| v.checked_sub(fee))
                    .map_err(arithmetic)?;
                holding.quantity = holding.quantity.checked_sub(quantity).map_err(arithmetic)?;
            }
            holding.revalue()?;
            dirty = true;
        }
        self.consumed_fills = fills.len();
        for (symbol, mark) in marks {
            let mark = Decimal18::from_decimal(mark).map_err(arithmetic)?;
            let holding = if let Some(holding) = self.holdings.get_mut(symbol) {
                holding
            } else {
                self.holdings.entry(symbol.into()).or_default()
            };
            if holding.mark != Some(mark) {
                holding.mark = Some(mark);
                if !holding.quantity.is_zero() {
                    holding.revalue()?;
                    dirty = true;
                }
            }
        }
        if !dirty {
            self.current.end_timestamp_ns = timestamp_ns;
            self.current.samples += 1;
            self.samples += 1;
            return Ok(());
        }
        let mut market_value = Decimal18::ZERO;
        let mut gross_exposure = Decimal18::ZERO;
        for holding in self.holdings.values() {
            market_value = market_value
                .checked_add(holding.value)
                .map_err(arithmetic)?;
            gross_exposure = gross_exposure
                .checked_add(holding.value.abs())
                .map_err(arithmetic)?;
        }
        self.peak_gross_exposure = self.peak_gross_exposure.max(gross_exposure);
        let next = ValuationRow {
            timestamp_ns,
            end_timestamp_ns: timestamp_ns,
            sample_index: self.samples,
            samples: 1,
            cash: self.cash,
            equity: self.cash.checked_add(market_value).map_err(arithmetic)?,
            market_value,
            position_count: self
                .holdings
                .values()
                .filter(|h| !h.quantity.is_zero())
                .count() as u32,
            fill_count: fills.len() as u64,
        };
        self.samples += 1;
        if self.current.same_state(&next) {
            self.current.end_timestamp_ns = timestamp_ns;
            self.current.samples += 1;
        } else {
            self.rows
                .push(std::mem::replace(&mut self.current, next))
                .map_err(|e| BacktestError::DataPreflight(e.to_string()))?;
        }
        Ok(())
    }

    pub fn finish(mut self) -> Result<ValuationHistory, BacktestError> {
        self.rows
            .push(self.current)
            .map_err(|e| BacktestError::DataPreflight(e.to_string()))?;
        Ok(ValuationHistory {
            rows: self.rows,
            samples: self.samples,
            peak_gross_exposure: self.peak_gross_exposure,
        })
    }
}

/// Immutable compact financial states, with exact logical sample counts.
#[derive(Clone, Debug)]
pub struct ValuationHistory {
    rows: History<ValuationRow>,
    samples: u64,
    peak_gross_exposure: Decimal18,
}

/// Execution scratch for rounded returns, including exact zero-return weights.
/// Errors need no encoded placeholder: an overflow makes the mean undefined,
/// and that same mean controls whether variance can consume any cached value.
#[derive(Clone)]
struct ReturnRun {
    value: Decimal18,
    samples: u64,
    flat: u64,
}

impl Record for ReturnRun {
    fn encode(&self, output: &mut Vec<u8>) {
        output.extend(self.value.unscaled().to_le_bytes());
        output.extend(self.samples.to_le_bytes());
        output.extend(self.flat.to_le_bytes());
    }

    fn decode(input: &mut &[u8]) -> std::io::Result<Self> {
        Ok(Self {
            value: Decimal18::from_unscaled(i128::from_le_bytes(take(input)?))
                .map_err(std::io::Error::other)?,
            samples: u64::from_le_bytes(take(input)?),
            flat: u64::from_le_bytes(take(input)?),
        })
    }
}

impl ValuationHistory {
    /// Maximum sum of absolute marked holdings across every causal state.
    pub fn peak_gross_exposure(&self) -> Decimal18 {
        self.peak_gross_exposure
    }
    pub fn sample_count(&self) -> u64 {
        self.samples
    }

    pub fn iter(
        &self,
    ) -> std::io::Result<impl Iterator<Item = std::io::Result<ValuationRow>> + '_> {
        self.rows.iter()
    }

    /// Preserve full-resolution metric conventions independently of output cadence.
    pub fn summarize(&self) -> std::io::Result<(EquityMetrics, History<ValuationRow>)> {
        let mut summary = EquityAccumulator::default();
        let mut sampler = ReportSampler {
            output: History::new(self.rows.resources()),
            ..ReportSampler::default()
        };
        let mut returns = History::new(self.rows.resources());
        for row in self.iter()? {
            let row = row?;
            let sample = summary.push_and_return(row.point());
            let (value, mut samples) = match sample {
                Some(Ok(value)) => (value, 1),
                _ => (Decimal18::ZERO, 0),
            };
            let mut flat = row.samples - 1;
            if value.is_zero() {
                samples += flat;
                flat = 0;
            }
            // One return record per valuation record keeps the spill threshold
            // and caller-supplied scratch requirement aligned with the source.
            returns.push(ReturnRun {
                value,
                samples,
                flat,
            })?;
            summary.extend_flat(row.end_timestamp_ns, row.samples as usize - 1);
            sampler.push(row.first())?;
            if row.samples > 1 {
                sampler.push(row.endpoint())?;
            }
        }
        let mut variance = summary.variance();
        for run in returns.iter()? {
            let run = run?;
            variance.add_returns(Ok(run.value), run.samples);
            variance.add_returns(Ok(Decimal18::ZERO), run.flat);
        }
        Ok((summary.finish(variance), sampler.finish()?))
    }
}

#[derive(Default)]
struct ReportSampler {
    bucket: Option<i64>,
    first: Option<ValuationRow>,
    low: Option<ValuationRow>,
    high: Option<ValuationRow>,
    last: Option<ValuationRow>,
    fills: Vec<ValuationRow>,
    output: History<ValuationRow>,
}

impl ReportSampler {
    fn push(&mut self, row: ValuationRow) -> std::io::Result<()> {
        let bucket = row.timestamp_ns.div_euclid(REPORT_INTERVAL_NS);
        let previous_fills = self.last.as_ref().map_or(0, |last| last.fill_count);
        if self.bucket != Some(bucket) {
            self.flush()?;
            self.bucket = Some(bucket);
        }
        if self.first.is_none() {
            self.first = Some(row.clone());
        }
        if self.low.as_ref().is_none_or(|low| row.equity < low.equity) {
            self.low = Some(row.clone());
        }
        if self
            .high
            .as_ref()
            .is_none_or(|high| row.equity > high.equity)
        {
            self.high = Some(row.clone());
        }
        if row.fill_count != previous_fills {
            self.fills.push(row.clone());
        }
        self.last = Some(row);
        Ok(())
    }

    fn flush(&mut self) -> std::io::Result<()> {
        let mut points: Vec<_> = [
            self.first.take(),
            self.low.take(),
            self.high.take(),
            self.last.take(),
        ]
        .into_iter()
        .flatten()
        .collect();
        points.append(&mut self.fills);
        points.sort_by_key(|row| row.sample_index);
        points.dedup_by_key(|row| row.sample_index);
        for point in points {
            self.output.push(point)?;
        }
        Ok(())
    }

    fn finish(mut self) -> std::io::Result<History<ValuationRow>> {
        self.flush()?;
        Ok(self.output)
    }
}
