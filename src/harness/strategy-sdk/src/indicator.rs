//! Composable, symbol-aware indicators with causal output sequences.
use crate::BarFrame;
use crate::MarketConsumer;
use crate::ObservationContext;
use num::Decimal;
use std::collections::BTreeMap;

/// An observation-only indicator. Routing records outputs after dependencies
/// and callbacks update, before the owning program sees the event.
pub trait Indicator: MarketConsumer<Context = ObservationContext> {
    /// Current value for this symbol; `None` denotes warm-up or undefined output.
    fn value(&self, symbol: &str) -> Option<Decimal>;
    /// Complete output history and separately held open tick windows.
    fn sequence(&self) -> &IndicatorSequence;
    /// Router-owned recording surface. Do not change its cadence during a run.
    fn sequence_mut(&mut self) -> &mut IndicatorSequence;
}

/// Why a completed output has no value.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum IndicatorMissing {
    /// The requested input bar was absent.
    MissingInput,
    /// No output was defined for one or more accepted observations.
    Undefined,
}

/// One output bar. Tick OHLC summarizes successive indicator values, rather
/// than the source trade prices. No empty windows or carry-forward are invented.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct IndicatorBar {
    /// Inclusive source/window start.
    pub start_ns: i64,
    /// Exclusive source/window end.
    pub end_ns: i64,
    /// Time this completed output became observable; absent for an open window.
    pub available_at_ns: Option<i64>,
    /// First defined value.
    pub open: Option<Decimal>,
    /// Maximum defined value.
    pub high: Option<Decimal>,
    /// Minimum defined value.
    pub low: Option<Decimal>,
    /// Last accepted value, including undefined output.
    pub close: Option<Decimal>,
    /// Number of accepted input observations.
    pub observations: u64,
    /// Explicit missing/undefined state, even if part of a window was defined.
    pub missing: Option<IndicatorMissing>,
}

/// Full causal output history keyed by (symbol, cadence). Bar cadence is the
/// input interval; tick cadence is `ticks:<nanoseconds>`. A default sequence
/// supports bars only. Tick consumers must request a positive output cadence.
#[derive(Clone, Debug, Default)]
pub struct IndicatorSequence {
    tick_interval_ns: Option<i64>,
    completed: BTreeMap<(String, String), Vec<IndicatorBar>>,
    in_progress: BTreeMap<String, IndicatorBar>,
    clock_ns: Option<i64>,
}
impl IndicatorSequence {
    /// Configure tick output fidelity in epoch-aligned nanosecond windows.
    pub fn ticks(interval_ns: i64) -> Result<Self, String> {
        if interval_ns <= 0 {
            return Err(format!(
                "IndicatorSequence::ticks interval_ns={interval_ns}: expected positive nanoseconds"
            ));
        }
        Ok(Self {
            tick_interval_ns: Some(interval_ns),
            ..Self::default()
        })
    }
    /// Completed outputs by symbol and cadence, in observation order.
    pub fn completed(&self) -> &BTreeMap<(String, String), Vec<IndicatorBar>> {
        &self.completed
    }
    /// Open tick outputs, never included in completed history.
    pub fn in_progress(&self) -> &BTreeMap<String, IndicatorBar> {
        &self.in_progress
    }
    /// Requested tick cadence.
    pub fn tick_interval_ns(&self) -> Option<i64> {
        self.tick_interval_ns
    }
    /// Advance only to an actually observed causal time. This seals populated
    /// windows whose end has passed and leaves the final partial window open.
    pub fn advance(&mut self, available_at_ns: i64) -> Result<(), String> {
        if self.clock_ns.is_some_and(|clock| available_at_ns < clock) {
            return Err(format!("indicator available_at_ns={available_at_ns} regressed below {:?}; observations must be in causal time order", self.clock_ns));
        }
        self.clock_ns = Some(available_at_ns);
        let ready: Vec<_> = self
            .in_progress
            .iter()
            .filter(|(_, bar)| bar.end_ns <= available_at_ns)
            .map(|(symbol, _)| symbol.clone())
            .collect();
        for symbol in ready {
            let mut bar = self
                .in_progress
                .remove(&symbol)
                .expect("collected open window");
            bar.available_at_ns = Some(available_at_ns);
            let cadence = format!(
                "ticks:{}",
                self.tick_interval_ns.expect("open tick cadence")
            );
            self.completed
                .entry((symbol, cadence))
                .or_default()
                .push(bar);
        }
        Ok(())
    }
    /// Record one completed bar-derived output, preserving source boundaries.
    pub fn record_bar(
        &mut self,
        symbol: &str,
        frame: &BarFrame,
        value: Option<Decimal>,
    ) -> Result<(), String> {
        let (interval, start_ns, end_ns, available_at_ns) = (
            frame.interval,
            frame.interval_start_ns,
            frame.interval_end_ns,
            frame.available_at_ns,
        );
        let missing_input = frame.missing_symbols.contains(&symbol);
        if !missing_input && !frame.bars_by_symbol.contains_key(symbol) {
            return Err(format!("indicator symbol {symbol:?} is neither present nor explicitly missing in frame {interval:?} at {available_at_ns}"));
        }
        if start_ns >= end_ns || available_at_ns < end_ns {
            return Err(format!("indicator {symbol:?}/{interval}: require start_ns < end_ns <= available_at_ns; received {start_ns}, {end_ns}, {available_at_ns}"));
        }
        self.advance(available_at_ns)?;
        let bars = self
            .completed
            .entry((symbol.into(), interval.into()))
            .or_default();
        if bars.last().is_some_and(|bar| start_ns < bar.end_ns) {
            return Err(format!("indicator {symbol:?}/{interval}: start_ns={start_ns} overlaps the previous completed output ending at {}", bars.last().expect("checked nonempty").end_ns));
        }
        let value = if missing_input { None } else { value };
        bars.push(IndicatorBar {
            start_ns,
            end_ns,
            available_at_ns: Some(available_at_ns),
            open: value,
            high: value,
            low: value,
            close: value,
            observations: if missing_input { 0 } else { 1 },
            missing: if missing_input {
                Some(IndicatorMissing::MissingInput)
            } else if value.is_none() {
                Some(IndicatorMissing::Undefined)
            } else {
                None
            },
        });
        Ok(())
    }
    /// Record an accepted trade/quote output. Values are never pooled across symbols.
    pub fn record_tick(
        &mut self,
        symbol: &str,
        timestamp_ns: i64,
        value: Option<Decimal>,
    ) -> Result<(), String> {
        let interval = self
            .tick_interval_ns
            .ok_or("tick indicator must declare output fidelity with IndicatorSequence::ticks")?;
        let start_ns = timestamp_ns
            .div_euclid(interval)
            .checked_mul(interval)
            .ok_or("indicator window overflow")?;
        let end_ns = start_ns
            .checked_add(interval)
            .ok_or("indicator window overflow")?;
        self.advance(timestamp_ns)?;
        let bar = self
            .in_progress
            .entry(symbol.into())
            .or_insert(IndicatorBar {
                start_ns,
                end_ns,
                available_at_ns: None,
                open: None,
                high: None,
                low: None,
                close: None,
                observations: 0,
                missing: None,
            });
        bar.observations += 1;
        if let Some(value) = value {
            bar.open.get_or_insert(value);
            bar.high = Some(bar.high.map_or(value, |high| high.max(value)));
            bar.low = Some(bar.low.map_or(value, |low| low.min(value)));
        } else {
            bar.missing = Some(IndicatorMissing::Undefined);
        }
        bar.close = value;
        Ok(())
    }
}

/// The precision of the pinned indicator arithmetic context.
pub const INDICATOR_CONTEXT_PRECISION: u32 = num::INDICATOR_PRECISION;

/// The pinned indicator arithmetic context (34 significant digits).
#[must_use]
pub const fn indicator_context() -> num::Context {
    num::Context::new(num::INDICATOR_PRECISION, num::RoundingMode::HalfEven)
}

/// Add two decimals inside the pinned indicator context.
///
/// # Errors
///
/// Returns an error when the exact sum does not fit the context.
pub fn indicator_add(left: Decimal, right: Decimal) -> Result<Decimal, num::DecimalError> {
    left.checked_add_with(right, &indicator_context())
}

/// Subtract two decimals inside the pinned indicator context.
///
/// # Errors
///
/// Returns an error when the exact difference does not fit the context.
pub fn indicator_sub(left: Decimal, right: Decimal) -> Result<Decimal, num::DecimalError> {
    left.checked_sub_with(right, &indicator_context())
}

/// Multiply two decimals inside the pinned indicator context.
///
/// # Errors
///
/// Returns an error when the exact product does not fit the context.
pub fn indicator_mul(left: Decimal, right: Decimal) -> Result<Decimal, num::DecimalError> {
    left.checked_mul_with(right, &indicator_context())
}

/// Divide two decimals inside the pinned indicator context.
///
/// # Errors
///
/// Returns an error when the quotient cannot be represented.
pub fn indicator_div(left: Decimal, right: Decimal) -> Result<Decimal, num::DecimalError> {
    left.checked_div_with(right, &indicator_context())
}
