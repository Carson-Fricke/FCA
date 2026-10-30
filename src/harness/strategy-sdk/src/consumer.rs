//! Shared subscription declarations and dependency-first market routing.
//!
//! Individual options use the same quote, trade, and bar subscriptions as stocks.
//! Subscribe to an input alias (for example `ATM`); the submitted configuration's
//! `data.options.ATM` selects an underlying/right and either expiration or
//! days_to_expiration, plus strike or absolute delta. Selection occurs once at
//! explicit `as_of` no later than replay start. Callbacks receive the alias;
//! acquisition evidence retains the actual OSI contract, multiplier, and model
//! assumptions. Delta selection estimates a European-model delta from historical
//! quotes; it does not roll the contract or supply a chain/Greek subscription.

pub use harness_contracts::{canonical_subscriptions, Subscription};
use std::collections::{BTreeMap, BTreeSet};

use crate::BarFrame;
use crate::Indicator;
use crate::QuoteEvent;
use crate::TradeEvent;

/// Observation-only callback context, with no order submission capability.
///
/// ```compile_fail
/// use harness_strategy_sdk::ObservationContext;
/// let mut context = ObservationContext;
/// context.submit(unsafe { std::mem::zeroed() });
/// ```
#[derive(Default)]
pub struct ObservationContext;

/// The common authoring contract for studies, strategies, and indicators.
pub trait MarketConsumer {
    /// Capability supplied to this consumer's market callbacks.
    type Context;

    /// Stable identity during a traversal, including nested first-field objects.
    #[doc(hidden)]
    fn consumer_identity(&self) -> (usize, &'static str) {
        (
            (self as *const Self).cast::<()>() as usize,
            std::any::type_name::<Self>(),
        )
    }

    /// Only this consumer's inputs, excluding its indicators' inputs.
    /// Requirements must remain stable after construction for the run.
    fn subscriptions(&self) -> Vec<Subscription>;

    /// Composable, program-owned dependencies, updated before this consumer.
    /// Topology must remain stable; zero-sized dependencies and cycles are rejected.
    fn indicators(&mut self) -> Vec<&mut dyn Indicator> {
        Vec::new()
    }

    /// Observe one requested quote.
    fn on_quote(&mut self, _event: &QuoteEvent, _context: &mut Self::Context) {}
    /// Observe one requested trade.
    fn on_trade(&mut self, _event: &TradeEvent, _context: &mut Self::Context) {}
    /// Observe one atomic frame, restricted to requested symbols and interval.
    fn on_bar(&mut self, _frame: &BarFrame, _context: &mut Self::Context) {}

    /// Seal features after the root program and dependencies consume a replay
    /// frame. Called once per effective frame, only on the program root.
    /// This borrowed capability cannot be retained or used from `on_stop`.
    fn on_decision(&mut self, _context: &mut crate::DecisionContext<'_>) {}
}

type Identity = (usize, &'static str);

#[derive(Clone, Debug, PartialEq, Eq)]
struct RoutingSignature {
    identity: Identity,
    slot: usize,
    declared: Vec<Subscription>,
    own: Vec<Subscription>,
    tick_interval_ns: Option<i64>,
    dependencies: Vec<RoutingSignature>,
}

/// A reusable, consumer-bound routing plan with execution-local traversal storage.
/// Construct after the consumer reaches its final address. Declarations, dependency
/// identity/order and output cadence remain checked before and after callbacks;
/// dispatch never rebuilds this plan or retains borrowed indicator pointers.
pub struct RoutingPlan {
    root: RoutingSignature,
    visited: Vec<bool>,
}

impl RoutingPlan {
    /// Capture stable declarations and dependency order at the final consumer
    /// address. Reject invalid subscriptions, cycles, zero-sized dependencies
    /// and tick indicators without a positive `IndicatorSequence::ticks` cadence.
    pub fn new<C: MarketConsumer + ?Sized>(consumer: &mut C) -> Result<Self, String> {
        let mut slots = BTreeMap::new();
        let root = collect(consumer, &mut BTreeSet::new(), &mut slots)?;
        Ok(Self {
            root,
            visited: vec![false; slots.len()],
        })
    }

    pub(crate) fn subscriptions(&self) -> Result<Vec<Subscription>, String> {
        fn inputs(signature: &RoutingSignature, output: &mut Vec<Subscription>) {
            output.extend(signature.own.iter().cloned());
            for dependency in &signature.dependencies {
                inputs(dependency, output);
            }
        }
        let mut output = Vec::new();
        inputs(&self.root, &mut output);
        canonical_subscriptions(output)
    }
}

fn collect<C: MarketConsumer + ?Sized>(
    consumer: &mut C,
    visiting: &mut BTreeSet<Identity>,
    slots: &mut BTreeMap<Identity, usize>,
) -> Result<RoutingSignature, String> {
    let identity = consumer.consumer_identity();
    if !visiting.insert(identity) {
        return Err(format!("indicator dependency cycle at consumer type {:?}; dependencies must form a stable acyclic graph", identity.1));
    }
    let next_slot = slots.len();
    let slot = *slots.entry(identity).or_insert(next_slot);
    let declared = consumer.subscriptions();
    let own = canonical_subscriptions(declared.clone())?;
    let mut dependencies = Vec::new();
    for dependency in consumer.indicators() {
        if std::mem::size_of_val(dependency) == 0 {
            return Err(format!("consumer {:?}: zero-sized indicator dependency {:?}; own nonzero-sized stable indicator state", identity.1, dependency.consumer_identity().1));
        }
        let mut signature = collect(dependency, visiting, slots)?;
        signature.tick_interval_ns = dependency.sequence().tick_interval_ns();
        fn needs_ticks(signature: &RoutingSignature) -> bool {
            signature
                .own
                .iter()
                .any(|subscription| !matches!(subscription, Subscription::Bars { .. }))
                || signature.dependencies.iter().any(needs_ticks)
        }
        if needs_ticks(&signature) && signature.tick_interval_ns.is_none() {
            return Err(
                "tick indicator must declare output fidelity with IndicatorSequence::ticks".into(),
            );
        }
        dependencies.push(signature);
    }
    visiting.remove(&identity);
    Ok(RoutingSignature {
        identity,
        slot,
        declared,
        own,
        tick_interval_ns: None,
        dependencies,
    })
}

pub(crate) fn validate_routing<C: MarketConsumer + ?Sized>(
    consumer: &mut C,
    expected: &RoutingPlan,
) -> Result<(), String> {
    validate_node(consumer, &expected.root)
}

fn changed() -> String {
    "market consumer subscriptions or dependency topology changed after construction".into()
}

fn validate_node<C: MarketConsumer + ?Sized>(
    consumer: &mut C,
    expected: &RoutingSignature,
) -> Result<(), String> {
    if consumer.consumer_identity() != expected.identity {
        return Err(changed());
    }
    let declared = consumer.subscriptions();
    // Stable declarations take the comparison-only path. A semantically equal
    // permutation remains valid, without changing the captured plan.
    if declared != expected.declared && canonical_subscriptions(declared)? != expected.own {
        return Err(changed());
    }
    let dependencies = consumer.indicators();
    if dependencies.len() != expected.dependencies.len() {
        return Err(changed());
    }
    for (dependency, signature) in dependencies.into_iter().zip(&expected.dependencies) {
        if std::mem::size_of_val(dependency) == 0
            || dependency.consumer_identity() != signature.identity
            || dependency.sequence().tick_interval_ns() != signature.tick_interval_ns
        {
            return Err(changed());
        }
        validate_node(dependency, signature)?;
    }
    Ok(())
}

/// Union this consumer's and all nested indicators' exact requirements.
pub fn effective_subscriptions<C: MarketConsumer + ?Sized>(
    consumer: &mut C,
) -> Result<Vec<Subscription>, String> {
    RoutingPlan::new(consumer)?.subscriptions()
}

enum Input<'a, 'b> {
    Quote(&'a QuoteEvent<'b>),
    Trade(&'a TradeEvent<'b>),
    Bar(&'a BarFrame<'b>),
}

fn input_time(input: &Input<'_, '_>) -> i64 {
    match input {
        Input::Quote(event) => event.timestamp_ns,
        Input::Trade(event) => event.timestamp_ns,
        Input::Bar(frame) => frame.available_at_ns,
    }
}
fn matches_symbol(
    signature: &RoutingSignature,
    symbol: &str,
    kind: &str,
    interval: Option<&str>,
) -> bool {
    signature.own.iter().any(|subscription| match subscription {
        Subscription::Quote { symbols } => kind == "quote" && symbols.iter().any(|s| s == symbol),
        Subscription::Trade { symbols } => kind == "trade" && symbols.iter().any(|s| s == symbol),
        Subscription::Bars {
            symbols,
            interval: cadence,
        } => {
            kind == "bars"
                && interval == Some(cadence.as_str())
                && symbols.iter().any(|s| s == symbol)
        }
    }) || signature
        .dependencies
        .iter()
        .any(|dependency| matches_symbol(dependency, symbol, kind, interval))
}
fn capture(
    indicator: &mut dyn Indicator,
    input: &Input<'_, '_>,
    signature: &RoutingSignature,
) -> Result<(), String> {
    match input {
        Input::Quote(event) if matches_symbol(signature, event.symbol, "quote", None) => {
            let value = indicator.value(event.symbol);
            indicator
                .sequence_mut()
                .record_tick(event.symbol, event.timestamp_ns, value)?;
        }
        Input::Trade(event) if matches_symbol(signature, event.symbol, "trade", None) => {
            let value = indicator.value(event.symbol);
            indicator
                .sequence_mut()
                .record_tick(event.symbol, event.timestamp_ns, value)?;
        }
        Input::Bar(frame) => {
            for symbol in frame
                .bars_by_symbol
                .keys()
                .copied()
                .chain(frame.missing_symbols.iter().copied())
            {
                if matches_symbol(signature, symbol, "bars", Some(frame.interval)) {
                    let missing = frame.missing_symbols.contains(&symbol);
                    let value = if missing {
                        None
                    } else {
                        indicator.value(symbol)
                    };
                    indicator.sequence_mut().record_bar(symbol, frame, value)?;
                }
            }
        }
        _ => {}
    }
    Ok(())
}

fn route<C: MarketConsumer + ?Sized>(
    consumer: &mut C,
    input: &Input<'_, '_>,
    context: &mut C::Context,
    seen: &mut [bool],
    expected: &RoutingSignature,
) -> Result<(), String> {
    validate_node(consumer, expected)?;
    if seen[expected.slot] {
        return Ok(());
    }
    seen[expected.slot] = true;
    for (dependency, signature) in consumer
        .indicators()
        .into_iter()
        .zip(&expected.dependencies)
    {
        let first_visit = !seen[signature.slot];
        dependency.sequence_mut().advance(input_time(input))?;
        route(dependency, input, &mut ObservationContext, seen, signature)?;
        if first_visit {
            capture(dependency, input, signature)?;
        }
    }
    validate_node(consumer, expected)?;
    let subscriptions = &expected.own;
    match input {
        Input::Quote(event) => {
            if subscriptions.iter().any(|s| matches!(s, Subscription::Quote { symbols } if symbols.iter().any(|s| s == event.symbol))) {
                consumer.on_quote(event, context);
            }
        }
        Input::Trade(event) => {
            if subscriptions.iter().any(|s| matches!(s, Subscription::Trade { symbols } if symbols.iter().any(|s| s == event.symbol))) {
                consumer.on_trade(event, context);
            }
        }
        Input::Bar(frame) => {
            let symbols: BTreeSet<&str> = subscriptions.iter().filter_map(|s| match s {
                Subscription::Bars { symbols, interval } if interval == frame.interval => Some(symbols),
                _ => None,
            }).flatten().map(String::as_str).collect();
            let mut view = (*frame).clone();
            view.bars_by_symbol.retain(|symbol, _| symbols.contains(symbol));
            view.missing_symbols.retain(|symbol| symbols.contains(symbol));
            if !view.bars_by_symbol.is_empty() || !view.missing_symbols.is_empty() {
                consumer.on_bar(&view, context);
            }
        }
    }
    validate_node(consumer, expected)
}

/// Route a quote dependency-first, never broadcasting unrequested inputs.
pub fn route_quote<C: MarketConsumer + ?Sized>(
    consumer: &mut C,
    event: &QuoteEvent,
    context: &mut C::Context,
    plan: &mut RoutingPlan,
) -> Result<(), String> {
    plan.visited.fill(false);
    route(
        consumer,
        &Input::Quote(event),
        context,
        &mut plan.visited,
        &plan.root,
    )
}
/// Route a trade dependency-first, never broadcasting unrequested inputs.
pub fn route_trade<C: MarketConsumer + ?Sized>(
    consumer: &mut C,
    event: &TradeEvent,
    context: &mut C::Context,
    plan: &mut RoutingPlan,
) -> Result<(), String> {
    plan.visited.fill(false);
    route(
        consumer,
        &Input::Trade(event),
        context,
        &mut plan.visited,
        &plan.root,
    )
}
/// Route an atomic frame dependency-first, filtering interval and symbols.
pub fn route_bar<C: MarketConsumer + ?Sized>(
    consumer: &mut C,
    frame: &BarFrame,
    context: &mut C::Context,
    plan: &mut RoutingPlan,
) -> Result<(), String> {
    plan.visited.fill(false);
    route(
        consumer,
        &Input::Bar(frame),
        context,
        &mut plan.visited,
        &plan.root,
    )
}
