//! The paper-trading shell and its restorable supervision surface.
//!
//! Port of the deterministic core of `harness.runtime.paper`: the session
//! policy and options records, a concrete in-memory [`PaperRuntimeShell`] that
//! composes virtual books, marks, venue watermarks, per-strategy order counts,
//! and exported strategy state, and the [`PaperShell`]/[`PaperShellRestore`]
//! traits the state store captures through. The full Python shell also
//! composes replay, the risk pipeline, and the strategy loader; those live in
//! other migration epics, so the shell here is driven by explicit,
//! deterministic mutators and never reads a wall clock.

use std::collections::BTreeMap;

use harness_num::Decimal18;

use crate::backtest::errors::{AbortPolicy, FillModel, StrategyHaltPolicy, TeardownPolicy};
use crate::backtest::fills::FillModelOptions;
use crate::canonical_json::JsonValue;
use crate::runtime::state_store::FillState;

/// Session boundary policy for a paper run.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PaperSessionPolicy {
    /// One continuous session.
    Continuous,
    /// Stop at the configured session close.
    Session,
}

impl PaperSessionPolicy {
    /// The stable lowercase value.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::Continuous => "continuous",
            Self::Session => "session",
        }
    }

    /// Parse a session policy from its lowercase value.
    #[must_use]
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "continuous" => Some(Self::Continuous),
            "session" => Some(Self::Session),
            _ => None,
        }
    }
}

/// Deterministic options for one paper run.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PaperOptions {
    /// Run identifier.
    pub run_id: String,
    /// String-exact initial cash.
    pub initial_cash: String,
    /// Selected fill model.
    pub fill_model: FillModel,
    /// Fill model options.
    pub fill_options: Option<FillModelOptions>,
    /// String-exact fee precision.
    pub fee_precision: String,
    /// Abort policy.
    pub abort_policy: AbortPolicy,
    /// Strategy-halt policy.
    pub strategy_halt_policy: StrategyHaltPolicy,
    /// Teardown policy.
    pub teardown_policy: TeardownPolicy,
    /// Session boundary policy.
    pub session_policy: PaperSessionPolicy,
}

impl PaperOptions {
    /// Build options with the Python defaults for one run.
    #[must_use]
    pub fn new(run_id: impl Into<String>) -> Self {
        Self {
            run_id: run_id.into(),
            initial_cash: "1000000".to_string(),
            fill_model: FillModel::LatencySize,
            fill_options: None,
            fee_precision: "0.0001".to_string(),
            abort_policy: AbortPolicy::Raise,
            strategy_halt_policy: StrategyHaltPolicy::HaltStrategy,
            teardown_policy: TeardownPolicy::Always,
            session_policy: PaperSessionPolicy::Session,
        }
    }
}

/// One paper session's terminal result, as seen by supervision.
#[derive(Clone, Debug, PartialEq, Eq, Default)]
pub struct PaperSessionResult {
    /// Events dispatched on the bus.
    pub dispatched_count: u64,
    /// Market events consumed.
    pub market_event_count: u64,
    /// Fills produced.
    pub fill_count: u64,
    /// Booked fills in application order.
    pub fills: Vec<FillState>,
}

/// The simulated venue's id and event-sequence counters at a capture point.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct VenueWatermarks {
    /// Venue identifier ordinal.
    pub id_ordinal: u64,
    /// Arena event sequence; `-1` precedes the first event.
    pub event_sequence: i64,
}

impl Default for VenueWatermarks {
    fn default() -> Self {
        Self {
            id_ordinal: 0,
            event_sequence: -1,
        }
    }
}

fn parse_decimal(value: &str) -> Decimal18 {
    value.parse::<Decimal18>().unwrap_or(Decimal18::ZERO)
}

/// One virtual book's restorable accounting state.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PaperBook {
    /// Book identifier.
    pub book_id: String,
    /// String-exact initial cash.
    pub initial_cash: String,
    /// String-exact current cash.
    pub cash: String,
    /// Captured position markers, sorted by symbol.
    pub markers: Vec<(String, String)>,
    /// Booked fills in application order.
    pub fills: Vec<FillState>,
}

impl PaperBook {
    /// Create an empty book with exact initial cash.
    #[must_use]
    pub fn new(book_id: impl Into<String>, initial_cash: impl Into<String>) -> Self {
        let book_id = book_id.into();
        let initial_cash = initial_cash.into();
        Self {
            book_id,
            initial_cash: initial_cash.clone(),
            cash: initial_cash,
            markers: Vec::new(),
            fills: Vec::new(),
        }
    }

    /// The booked fill identifiers in application order.
    #[must_use]
    pub fn fill_ids(&self) -> Vec<String> {
        self.fills.iter().map(|fill| fill.fill_id.clone()).collect()
    }

    /// Book one fill, updating exact cash and rejecting duplicate identifiers.
    ///
    /// # Errors
    ///
    /// Returns a message when the fill was already booked.
    pub fn apply_fill(&mut self, fill: &FillState) -> Result<(), String> {
        if self.fills.iter().any(|existing| existing.fill_id == fill.fill_id) {
            return Err(format!("book {} already accounts fill {}", self.book_id, fill.fill_id));
        }
        if let Some(marker) = self.markers.iter_mut().find(|(symbol, _)| *symbol == fill.symbol) {
            marker.1 = fill.price.clone();
        } else {
            self.markers.push((fill.symbol.clone(), fill.price.clone()));
        }
        let quantity = parse_decimal(&fill.quantity);
        let price = parse_decimal(&fill.price);
        let fee = parse_decimal(&fill.fee);
        let notional = quantity.checked_mul(price).unwrap_or(Decimal18::ZERO);
        let cash = parse_decimal(&self.cash);
        let cash = if fill.side == "sell" {
            cash.checked_add(notional)
                .and_then(|value| value.checked_sub(fee))
        } else {
            cash.checked_sub(notional)
                .and_then(|value| value.checked_sub(fee))
        }
        .unwrap_or(cash);
        self.cash = cash.to_string();
        self.fills.push(fill.clone());
        Ok(())
    }
}

/// The shell supervision surface the state store captures through.
pub trait PaperShell {
    /// The shell's run identifier.
    fn run_id(&self) -> &str;
    /// The shell's deterministic options.
    fn options(&self) -> &PaperOptions;
    /// The virtual books in configuration order.
    fn books(&self) -> &[PaperBook];
    /// The latest marks, string-exact, keyed by symbol.
    fn prices(&self) -> &BTreeMap<String, String>;
    /// The risk pipeline's rejection policy.
    fn on_reject(&self) -> &str;
    /// The configured risk rule identifiers in order.
    fn rule_ids(&self) -> &[String];
    /// Orders submitted by one strategy instance.
    fn order_count(&self, instance_id: &str) -> u64;
    /// String-exact daily loss for one strategy instance.
    fn daily_loss(&self, instance_id: &str) -> String;
    /// The current session result.
    fn result(&self) -> PaperSessionResult;
    /// Exported strategy state values, keyed by instance id.
    fn strategy_state_export(&self) -> BTreeMap<String, JsonValue>;
    /// The venue id and event-sequence watermarks.
    fn venue_watermarks(&self) -> VenueWatermarks;
}

/// The public mutators a snapshot restores through.
pub trait PaperShellRestore: PaperShell {
    /// Mutable access to one virtual book by identifier.
    fn shell_book_mut(&mut self, book_id: &str) -> Option<&mut PaperBook>;
    /// Replay the captured session fill history in sequence order.
    fn restore_fills(&mut self, fills: &[FillState]);
    /// Replace the dispatched and market-event session counters.
    fn restore_session_counts(&mut self, dispatched_count: u64, market_event_count: u64);
    /// Replace the risk-gate identity.
    fn restore_risk(&mut self, on_reject: &str, rule_ids: &[String]);
    /// Replace the per-strategy daily losses.
    fn restore_daily_losses(&mut self, losses: &BTreeMap<String, String>);
    /// Replace the latest marks.
    fn restore_prices(&mut self, prices: BTreeMap<String, String>);
    /// Increment one strategy instance's order count by one.
    fn count_order(&mut self, instance_id: &str);
    /// Attribute one order identifier to a strategy instance.
    fn own_order(&mut self, order_id: &str, instance_id: &str);
    /// Replace the exported strategy state.
    fn restore_strategy_state(&mut self, state: BTreeMap<String, JsonValue>);
    /// Replace the venue watermarks.
    fn restore_venue_watermarks(&mut self, watermarks: VenueWatermarks);
}

/// A concrete, deterministic paper shell over in-memory books.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PaperRuntimeShell {
    options: PaperOptions,
    session: PaperSessionResult,
    books: Vec<PaperBook>,
    prices: BTreeMap<String, String>,
    on_reject: String,
    rule_ids: Vec<String>,
    order_counts: BTreeMap<String, u64>,
    daily_losses: BTreeMap<String, String>,
    order_owners: BTreeMap<String, String>,
    strategy_state: BTreeMap<String, JsonValue>,
    venue: VenueWatermarks,
}

impl PaperRuntimeShell {
    /// Compose a shell with one book per `(book_id, initial_cash)` pair.
    #[must_use]
    pub fn new(options: PaperOptions, books: Vec<(String, String)>) -> Self {
        Self {
            options,
            session: PaperSessionResult::default(),
            books: books
                .into_iter()
                .map(|(book_id, initial_cash)| PaperBook::new(book_id, initial_cash))
                .collect(),
            prices: BTreeMap::new(),
            on_reject: "reject_order".to_string(),
            rule_ids: Vec::new(),
            order_counts: BTreeMap::new(),
            daily_losses: BTreeMap::new(),
            order_owners: BTreeMap::new(),
            strategy_state: BTreeMap::new(),
            venue: VenueWatermarks::default(),
        }
    }

    /// Set the risk pipeline identity.
    pub fn with_risk(mut self, on_reject: impl Into<String>, rule_ids: Vec<String>) -> Self {
        self.on_reject = on_reject.into();
        self.rule_ids = rule_ids;
        self
    }

    /// The mutable session result, so a driver can advance the counters.
    pub fn session_mut(&mut self) -> &mut PaperSessionResult {
        &mut self.session
    }

    /// Set the latest mark for one symbol.
    pub fn set_price(&mut self, symbol: impl Into<String>, price: impl Into<String>) {
        self.prices.insert(symbol.into(), price.into());
    }

    /// Book one fill into the named book and the session history.
    ///
    /// # Errors
    ///
    /// Returns a message when the book is unknown or already accounts the fill.
    pub fn apply_fill(&mut self, book_id: &str, fill: FillState) -> Result<(), String> {
        let Some(book) = self.books.iter_mut().find(|book| book.book_id == book_id) else {
            return Err(format!("unknown book {book_id}"));
        };
        book.apply_fill(&fill)?;
        self.session.fill_count += 1;
        self.session.fills.push(fill);
        Ok(())
    }

    /// Set the daily loss for one strategy instance.
    pub fn set_daily_loss(&mut self, instance_id: &str, loss: impl Into<String>) {
        self.daily_losses
            .insert(instance_id.to_string(), loss.into());
    }

    /// Set one exported strategy-state value.
    pub fn set_strategy_value(&mut self, instance_id: &str, key: &str, value: JsonValue) {
        let entry = self
            .strategy_state
            .entry(instance_id.to_string())
            .or_insert_with(|| JsonValue::Object(BTreeMap::new()));
        if let JsonValue::Object(values) = entry {
            values.insert(key.to_string(), value);
        }
    }

    /// Set the venue watermarks.
    pub fn set_venue(&mut self, watermarks: VenueWatermarks) {
        self.venue = watermarks;
    }

    /// Attribute one order to a strategy instance.
    pub fn attribute_order(&mut self, order_id: &str, instance_id: &str) {
        self.order_owners
            .insert(order_id.to_string(), instance_id.to_string());
    }

    /// The number of books in the shell.
    #[must_use]
    pub fn book_count(&self) -> usize {
        self.books.len()
    }
}

impl PaperShell for PaperRuntimeShell {
    fn run_id(&self) -> &str {
        &self.options.run_id
    }

    fn options(&self) -> &PaperOptions {
        &self.options
    }

    fn books(&self) -> &[PaperBook] {
        &self.books
    }

    fn prices(&self) -> &BTreeMap<String, String> {
        &self.prices
    }

    fn on_reject(&self) -> &str {
        &self.on_reject
    }

    fn rule_ids(&self) -> &[String] {
        &self.rule_ids
    }

    fn order_count(&self, instance_id: &str) -> u64 {
        self.order_counts.get(instance_id).copied().unwrap_or(0)
    }

    fn daily_loss(&self, instance_id: &str) -> String {
        self.daily_losses
            .get(instance_id)
            .cloned()
            .unwrap_or_else(|| "0".to_string())
    }

    fn result(&self) -> PaperSessionResult {
        self.session.clone()
    }

    fn strategy_state_export(&self) -> BTreeMap<String, JsonValue> {
        self.strategy_state.clone()
    }

    fn venue_watermarks(&self) -> VenueWatermarks {
        self.venue
    }
}

impl PaperShellRestore for PaperRuntimeShell {
    fn shell_book_mut(&mut self, book_id: &str) -> Option<&mut PaperBook> {
        self.books.iter_mut().find(|book| book.book_id == book_id)
    }

    fn restore_fills(&mut self, fills: &[FillState]) {
        self.session.fills = fills.to_vec();
        self.session.fill_count = fills.len() as u64;
    }

    fn restore_session_counts(&mut self, dispatched_count: u64, market_event_count: u64) {
        self.session.dispatched_count = dispatched_count;
        self.session.market_event_count = market_event_count;
    }

    fn restore_risk(&mut self, on_reject: &str, rule_ids: &[String]) {
        self.on_reject = on_reject.to_string();
        self.rule_ids = rule_ids.to_vec();
    }

    fn restore_daily_losses(&mut self, losses: &BTreeMap<String, String>) {
        self.daily_losses = losses.clone();
    }

    fn restore_prices(&mut self, prices: BTreeMap<String, String>) {
        self.prices = prices;
    }

    fn count_order(&mut self, instance_id: &str) {
        *self
            .order_counts
            .entry(instance_id.to_string())
            .or_insert(0) += 1;
    }

    fn own_order(&mut self, order_id: &str, instance_id: &str) {
        self.order_owners
            .insert(order_id.to_string(), instance_id.to_string());
    }

    fn restore_strategy_state(&mut self, state: BTreeMap<String, JsonValue>) {
        self.strategy_state = state;
    }

    fn restore_venue_watermarks(&mut self, watermarks: VenueWatermarks) {
        self.venue = watermarks;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fill(fill_id: &str, symbol: &str, side: &str, quantity: &str, price: &str) -> FillState {
        FillState {
            event_id: "e".to_string(),
            timestamp: "2024-01-02T14:30:00.000000Z".to_string(),
            sequence: 0,
            fill_id: fill_id.to_string(),
            order_id: format!("order-{fill_id}"),
            symbol: symbol.to_string(),
            side: side.to_string(),
            quantity: quantity.to_string(),
            price: price.to_string(),
            fee: "0".to_string(),
            liquidity: None,
        }
    }

    #[test]
    fn session_policy_round_trips() {
        for policy in [PaperSessionPolicy::Continuous, PaperSessionPolicy::Session] {
            assert_eq!(PaperSessionPolicy::parse(policy.value()), Some(policy));
        }
    }

    #[test]
    fn options_defaults_match_python() {
        let options = PaperOptions::new("run");
        assert_eq!(options.initial_cash, "1000000");
        assert_eq!(options.abort_policy, AbortPolicy::Raise);
        assert_eq!(options.session_policy, PaperSessionPolicy::Session);
    }

    #[test]
    fn book_applies_fills_and_tracks_marks() {
        let mut book = PaperBook::new("alpha", "100000");
        book.apply_fill(&fill("f1", "SPY", "buy", "2", "100")).expect("fill");
        assert_eq!(book.fills.len(), 1);
        assert_eq!(book.fill_ids(), vec!["f1".to_string()]);
        assert_eq!(book.markers, vec![("SPY".to_string(), "100".to_string())]);
        assert_eq!(book.cash, "99800.000000000000000000");
        assert!(book.apply_fill(&fill("f1", "SPY", "buy", "2", "100")).is_err());
    }

    #[test]
    fn shell_supervision_surface_reads_back() {
        let mut shell = PaperRuntimeShell::new(
            PaperOptions::new("run"),
            vec![("alpha".to_string(), "100000".to_string())],
        )
        .with_risk("reject_order", vec!["max_position".to_string()]);
        shell.session_mut().dispatched_count = 3;
        shell.session_mut().market_event_count = 9;
        shell
            .apply_fill("alpha", fill("f1", "SPY", "buy", "2", "100"))
            .expect("fill");
        shell.count_order("momentum");
        shell.attribute_order("order-f1", "momentum");
        shell.set_price("SPY", "100");
        shell.set_daily_loss("momentum", "0");
        shell.set_venue(VenueWatermarks {
            id_ordinal: 4,
            event_sequence: 9,
        });
        assert_eq!(shell.result().fill_count, 1);
        assert_eq!(shell.order_count("momentum"), 1);
        assert_eq!(shell.venue_watermarks().event_sequence, 9);
        assert_eq!(shell.on_reject(), "reject_order");
    }
}
