//! The deterministic backtest engine loop.
//!
//! The full Python engine composes the strategy runtime, the risk pipeline, the
//! portfolio hub, and the simulated venue. Those capabilities are owned by other
//! migration epics, so the Rust loop drives them through the [`StrategyRuntime`],
//! [`RiskModel`], and [`ExecutionModel`] traits. Every implementation is
//! synchronous and deterministic: the loop never reads wall time, opens a
//! network connection, or uses unseeded randomness, and it is exercised with
//! deterministic fakes in the tests.

use std::collections::BTreeMap;

use harness_num::Decimal18;

use crate::canonical_json::JsonValue;
use crate::sha256::Sha256;

use super::config::BacktestConfig;
use super::errors::{
    AbortPolicy, BacktestError, BacktestStatus, FillModel, StrategyHaltPolicy, TeardownPolicy,
};
use super::fees::{fee_schedule_from_config, DEFAULT_FEE_PRECISION};
use super::fills::{build_fill_model, fill_model_identity, FillModelOptions, FillPlugin};
use super::fixture::{MarketEvent, ReplayFingerprints, ReplayFixture, SyntheticDataOptions};
use super::manifest::{build_manifest, BacktestManifest, BacktestResult, StrategyIdentity};
use super::preflight::{preflight_data, resolve_data_source};

/// Explicit, reproducible knobs for one [`run_backtest`] call.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BacktestOptions {
    /// Selected fill plugin.
    pub fill_model: FillModel,
    /// Fill plugin options.
    pub fill_options: Option<FillModelOptions>,
    /// String-exact fee precision.
    pub fee_precision: String,
    /// Abort policy.
    pub abort_policy: AbortPolicy,
    /// Strategy-halt policy.
    pub strategy_halt_policy: StrategyHaltPolicy,
    /// Teardown policy.
    pub teardown_policy: TeardownPolicy,
    /// Synthetic fixture options.
    pub synthetic_options: Option<SyntheticDataOptions>,
}

impl Default for BacktestOptions {
    fn default() -> Self {
        Self {
            fill_model: FillModel::SimpleTouch,
            fill_options: None,
            fee_precision: DEFAULT_FEE_PRECISION.to_string(),
            abort_policy: AbortPolicy::Raise,
            strategy_halt_policy: StrategyHaltPolicy::HaltStrategy,
            teardown_policy: TeardownPolicy::Always,
            synthetic_options: None,
        }
    }
}

/// The side of an order intent.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OrderSide {
    /// Buy side.
    Buy,
    /// Sell side.
    Sell,
}

impl OrderSide {
    /// The stable lowercase value.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::Buy => "buy",
            Self::Sell => "sell",
        }
    }
}

/// One order intent produced by a strategy.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OrderIntent {
    /// Order identifier.
    pub order_id: String,
    /// Instrument symbol.
    pub symbol: String,
    /// Order side.
    pub side: OrderSide,
    /// String-exact quantity.
    pub quantity: String,
    /// String-exact limit price, if any.
    pub limit_price: Option<String>,
}

/// One deterministic fill produced by an execution model.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Fill {
    /// Fill identifier.
    pub fill_id: String,
    /// Order identifier.
    pub order_id: String,
    /// Instrument symbol.
    pub symbol: String,
    /// Fill side.
    pub side: OrderSide,
    /// String-exact quantity.
    pub quantity: String,
    /// String-exact price.
    pub price: String,
    /// String-exact fee.
    pub fee: String,
    /// Fill time in epoch nanoseconds.
    pub timestamp_ns: i64,
    /// Fill sequence.
    pub sequence: u64,
}

/// The risk model's verdict on one order intent.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RiskOutcome {
    /// The order is admitted unchanged.
    Approved,
    /// The order is rejected with a reason.
    Rejected(String),
}

/// A deterministic strategy runtime driven by the engine loop.
pub trait StrategyRuntime {
    /// Called once before the first market event.
    fn on_start(&mut self) {}
    /// Called for every market event; append order intents to `orders`.
    fn on_market(&mut self, event: &MarketEvent, orders: &mut Vec<OrderIntent>);
    /// Called once after the loop; return teardown errors.
    fn on_stop(&mut self) -> Vec<String> {
        Vec::new()
    }
}

/// A deterministic risk gate for order intents.
pub trait RiskModel {
    /// Evaluate one order intent against the current market event.
    fn evaluate(&mut self, order: &OrderIntent, event: &MarketEvent) -> RiskOutcome;
}

/// A deterministic execution model turning admitted intents into fills.
pub trait ExecutionModel {
    /// Produce fills for one admitted order intent.
    fn fills(&mut self, order: &OrderIntent, event: &MarketEvent) -> Vec<Fill>;
}

/// Run one deterministic backtest through the injected seams.
///
/// # Errors
///
/// Returns [`BacktestError`] when the fixture cannot be preflighted.
#[allow(clippy::too_many_arguments)]
pub fn run_engine(
    _configuration: &BacktestConfig,
    fixture: &ReplayFixture,
    preflight: &super::fixture::DataPreflight,
    manifest: BacktestManifest,
    options: &BacktestOptions,
    strategy: &mut dyn StrategyRuntime,
    risk: &mut dyn RiskModel,
    execution: &mut dyn ExecutionModel,
) -> Result<BacktestResult, BacktestError> {
    strategy.on_start();
    let mut market_event_count = 0u64;
    let mut order_count = 0u64;
    let mut fill_count = 0u64;
    let mut dispatched: Vec<JsonValue> = Vec::new();
    let mut fills: Vec<JsonValue> = Vec::new();
    let mut approved_orders: Vec<OrderIntent> = Vec::new();
    let mut produced_fills: Vec<Fill> = Vec::new();
    for event in &fixture.events {
        market_event_count += 1;
        let mut intents = Vec::new();
        strategy.on_market(event, &mut intents);
        for intent in intents {
            dispatched.push(order_to_json(&intent));
            match risk.evaluate(&intent, event) {
                RiskOutcome::Rejected(_) => {}
                RiskOutcome::Approved => {
                    order_count += 1;
                    approved_orders.push(intent.clone());
                    for fill in execution.fills(&intent, event) {
                        fill_count += 1;
                        fills.push(fill_to_json(&fill));
                        produced_fills.push(fill);
                    }
                }
            }
        }
    }
    let teardown_errors = match options.teardown_policy {
        TeardownPolicy::Never => Vec::new(),
        TeardownPolicy::Always | TeardownPolicy::OnSuccess => strategy.on_stop(),
    };
    let dispatch_hash = dispatch_hash(&dispatched, &fills);
    verify_replay(fixture, preflight, market_event_count)?;
    Ok(BacktestResult {
        manifest,
        status: BacktestStatus::Completed,
        reason: None,
        preflight: preflight.clone(),
        dispatch_hash,
        dispatched_count: dispatched.len() as u64,
        market_event_count,
        order_count,
        fill_count,
        risk_decision_count: 0,
        halted_strategies: Vec::new(),
        stopped_strategies: Vec::new(),
        teardown_errors,
        lookahead_violations: Vec::new(),
        orders: approved_orders,
        fills: produced_fills,
    })
}

/// Resolve the fixture, preflight it, build the manifest, and run the engine.
///
/// # Errors
///
/// Returns [`BacktestError`] when the configuration, fixture, or manifest is
/// invalid.
#[allow(clippy::too_many_arguments)]
pub fn run_backtest(
    configuration: &BacktestConfig,
    data_source: Option<&ReplayFixture>,
    options: &BacktestOptions,
    strategy: &mut dyn StrategyRuntime,
    risk: &mut dyn RiskModel,
    execution: &mut dyn ExecutionModel,
) -> Result<BacktestResult, BacktestError> {
    let fixture = resolve_data_source(
        configuration,
        data_source,
        options.synthetic_options.as_ref(),
    )?;
    let preflight = preflight_data(configuration, &fixture)?;
    let plugin: FillPlugin = build_fill_model(options.fill_model, options.fill_options.as_ref());
    let identity = fill_model_identity(&plugin);
    let schedule = fee_schedule_from_config(
        &configuration.fees,
        &options.fee_precision,
        "harness-config",
    )?;
    let identity_fill_model = match &identity {
        JsonValue::Object(entries) => entries
            .get("model")
            .and_then(|value| match value {
                JsonValue::String(text) => Some(text.clone()),
                _ => None,
            })
            .unwrap_or_else(|| options.fill_model.value().to_string()),
        _ => options.fill_model.value().to_string(),
    };
    let fee_identity = JsonValue::object([
        (
            "precision",
            JsonValue::String(options.fee_precision.clone()),
        ),
        (
            "schedule_id",
            JsonValue::String(schedule.schedule_id.to_string()),
        ),
    ]);
    let manifest = build_manifest(
        configuration,
        &fixture,
        &preflight,
        &identity_fill_model,
        &identity,
        &schedule.schedule_id,
        &fee_identity,
        &JsonValue::object([("pipeline", JsonValue::string("default"))]),
        strategy_identities(configuration),
    )?;
    run_engine(
        configuration,
        &fixture,
        &preflight,
        manifest,
        options,
        strategy,
        risk,
        execution,
    )
}

fn strategy_identities(configuration: &BacktestConfig) -> Vec<StrategyIdentity> {
    configuration
        .strategy_instances
        .iter()
        .map(|instance| StrategyIdentity {
            instance_id: instance.instance_id.clone(),
            implementation: instance.implementation.clone(),
            bundle_hash: instance.bundle_hash.clone(),
            parameter_hash: super::manifest::configuration_hash(&JsonValue::Object(
                instance
                    .parameters
                    .iter()
                    .map(|(key, value)| (key.clone(), value.clone()))
                    .collect(),
            )),
            symbols: instance.symbols.clone(),
            capital_fraction: instance.capital_fraction.clone(),
        })
        .collect()
}

fn order_to_json(order: &OrderIntent) -> JsonValue {
    JsonValue::object([
        ("order_id", JsonValue::String(order.order_id.clone())),
        ("symbol", JsonValue::String(order.symbol.clone())),
        ("side", JsonValue::string(order.side.value())),
        ("quantity", JsonValue::String(order.quantity.clone())),
        (
            "limit_price",
            match &order.limit_price {
                Some(price) => JsonValue::String(price.clone()),
                None => JsonValue::Null,
            },
        ),
    ])
}

fn fill_to_json(fill: &Fill) -> JsonValue {
    JsonValue::object([
        ("fill_id", JsonValue::String(fill.fill_id.clone())),
        ("order_id", JsonValue::String(fill.order_id.clone())),
        ("symbol", JsonValue::String(fill.symbol.clone())),
        ("side", JsonValue::string(fill.side.value())),
        ("quantity", JsonValue::String(fill.quantity.clone())),
        ("price", JsonValue::String(fill.price.clone())),
        ("fee", JsonValue::String(fill.fee.clone())),
        ("timestamp_ns", JsonValue::Int(fill.timestamp_ns)),
        ("sequence", JsonValue::UInt(fill.sequence)),
    ])
}

fn dispatch_hash(dispatched: &[JsonValue], fills: &[JsonValue]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(b"fca:harness-backtest:dispatch:v1\0");
    for value in dispatched {
        hasher.update(value.to_canonical_string().as_bytes());
        hasher.update(b"\n");
    }
    for value in fills {
        hasher.update(value.to_canonical_string().as_bytes());
        hasher.update(b"\n");
    }
    hasher.finalize_hex()
}

fn verify_replay(
    fixture: &ReplayFixture,
    preflight: &super::fixture::DataPreflight,
    market_event_count: u64,
) -> Result<(), BacktestError> {
    if market_event_count != preflight.event_count as u64 {
        return Err(BacktestError::DataPreflight(
            "replayed event count does not match the preflighted fixture".to_string(),
        ));
    }
    if fixture.fingerprints != preflight.fingerprints {
        return Err(BacktestError::DataPreflight(
            "replayed data fingerprint does not match the preflighted fixture".to_string(),
        ));
    }
    Ok(())
}

/// A no-op strategy used to exercise the loop without a strategy port.
#[derive(Debug, Default)]
pub struct NullStrategy;

impl StrategyRuntime for NullStrategy {
    fn on_market(&mut self, _event: &MarketEvent, _orders: &mut Vec<OrderIntent>) {}
}

/// A risk model that approves every order.
#[derive(Debug, Default)]
pub struct ApproveAll;

impl RiskModel for ApproveAll {
    fn evaluate(&mut self, _order: &OrderIntent, _event: &MarketEvent) -> RiskOutcome {
        RiskOutcome::Approved
    }
}

/// An execution model that fills every order at the quote midpoint.
#[derive(Debug, Default)]
pub struct ImmediateFill;

impl ExecutionModel for ImmediateFill {
    fn fills(&mut self, order: &OrderIntent, event: &MarketEvent) -> Vec<Fill> {
        let price = match event {
            MarketEvent::Quote { bid, ask, .. } => bid
                .checked_add(*ask)
                .and_then(|sum| sum.checked_div(Decimal18::from_i64(2)))
                .map_or_else(|_| bid.to_string(), |value| value.to_string()),
            MarketEvent::Trade { price, .. } => price.to_string(),
        };
        vec![Fill {
            fill_id: format!("fill-{}", order.order_id),
            order_id: order.order_id.clone(),
            symbol: order.symbol.clone(),
            side: order.side,
            quantity: order.quantity.clone(),
            price,
            fee: "0".to_string(),
            timestamp_ns: event.timestamp_ns(),
            sequence: 0,
        }]
    }
}

/// A strategy that emits one buy intent per trade event, for tests.
#[derive(Debug, Default)]
pub struct TradeBuyStrategy {
    /// Emitted order count.
    pub emitted: u64,
}

impl StrategyRuntime for TradeBuyStrategy {
    fn on_market(&mut self, event: &MarketEvent, orders: &mut Vec<OrderIntent>) {
        if let MarketEvent::Trade {
            sequence, symbol, ..
        } = event
        {
            orders.push(OrderIntent {
                order_id: format!("order-{sequence}"),
                symbol: symbol.clone(),
                side: OrderSide::Buy,
                quantity: "1".to_string(),
                limit_price: None,
            });
            self.emitted += 1;
        }
    }
}

/// Snapshot the fixtures a test cares about without pulling the full crate.
pub fn fingerprint_of(events: &[MarketEvent]) -> ReplayFingerprints {
    ReplayFingerprints::of_events(events)
}

/// A map of counts by key, used by fakes that tally orders.
pub type Counts = BTreeMap<String, u64>;

#[cfg(test)]
mod tests {
    use super::*;
    use crate::backtest::errors::ONE_DAY_NS;
    use crate::backtest::fixture::days_from_civil;

    fn config() -> BacktestConfig {
        let start = days_from_civil(2024, 1, 1) * ONE_DAY_NS;
        let end = days_from_civil(2024, 1, 3) * ONE_DAY_NS;
        BacktestConfig::new("run", start, end, 7).expect("config")
    }

    #[test]
    fn null_strategy_completes_with_no_orders() {
        let configuration = config();
        let options = BacktestOptions::default();
        let result = run_backtest(
            &configuration,
            None,
            &options,
            &mut NullStrategy,
            &mut ApproveAll,
            &mut ImmediateFill,
        )
        .expect("run");
        assert!(result.completed());
        assert_eq!(result.market_event_count, 36);
        assert_eq!(result.order_count, 0);
        assert_eq!(result.fill_count, 0);
        assert!(result.dispatch_hash.len() == 64);
    }

    #[test]
    fn trade_strategy_fills_every_trade() {
        let configuration = config();
        let options = BacktestOptions::default();
        let mut strategy = TradeBuyStrategy::default();
        let result = run_backtest(
            &configuration,
            None,
            &options,
            &mut strategy,
            &mut ApproveAll,
            &mut ImmediateFill,
        )
        .expect("run");
        assert_eq!(strategy.emitted, 12);
        assert_eq!(result.order_count, 12);
        assert_eq!(result.fill_count, 12);
    }

    #[test]
    fn engine_artifacts_are_deterministic() {
        let configuration = config();
        let options = BacktestOptions::default();
        let first = run_backtest(
            &configuration,
            None,
            &options,
            &mut TradeBuyStrategy::default(),
            &mut ApproveAll,
            &mut ImmediateFill,
        )
        .expect("first");
        let second = run_backtest(
            &configuration,
            None,
            &options,
            &mut TradeBuyStrategy::default(),
            &mut ApproveAll,
            &mut ImmediateFill,
        )
        .expect("second");
        assert_eq!(first.artifact_json(), second.artifact_json());
        assert_eq!(first.artifact_digest(), second.artifact_digest());
        assert_eq!(first.outcome_digest(), second.outcome_digest());
        assert_eq!(
            first.manifest.manifest_hash(),
            second.manifest.manifest_hash()
        );
    }

    #[test]
    fn rejecting_risk_model_suppresses_orders() {
        struct RejectAll;
        impl RiskModel for RejectAll {
            fn evaluate(&mut self, _order: &OrderIntent, _event: &MarketEvent) -> RiskOutcome {
                RiskOutcome::Rejected("no".to_string())
            }
        }
        let configuration = config();
        let options = BacktestOptions::default();
        let result = run_backtest(
            &configuration,
            None,
            &options,
            &mut TradeBuyStrategy::default(),
            &mut RejectAll,
            &mut ImmediateFill,
        )
        .expect("run");
        assert_eq!(result.dispatched_count, 12);
        assert_eq!(result.order_count, 0);
    }
}
