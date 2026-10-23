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
use std::collections::BTreeSet;

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
}

type Identity = (usize, &'static str);

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct RoutingSignature {
    identity: Identity,
    own: Vec<Subscription>,
    tick_interval_ns: Option<i64>,
    dependencies: Vec<RoutingSignature>,
}

fn collect<C: MarketConsumer + ?Sized>(
    consumer: &mut C,
    visiting: &mut BTreeSet<Identity>,
) -> Result<RoutingSignature, String> {
    let identity = consumer.consumer_identity();
    if !visiting.insert(identity) {
        return Err("indicator dependency cycle".into());
    }
    let own = canonical_subscriptions(consumer.subscriptions())?;
    let mut dependencies = Vec::new();
    for dependency in consumer.indicators() {
        if std::mem::size_of_val(dependency) == 0 {
            return Err("zero-sized indicator dependencies are not supported".into());
        }
        let mut signature = collect(dependency, visiting)?;
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
        own,
        tick_interval_ns: None,
        dependencies,
    })
}

pub(crate) fn routing_signature<C: MarketConsumer + ?Sized>(
    consumer: &mut C,
) -> Result<RoutingSignature, String> {
    collect(consumer, &mut BTreeSet::new())
}

pub(crate) fn validate_routing<C: MarketConsumer + ?Sized>(
    consumer: &mut C,
    expected: &RoutingSignature,
) -> Result<(), String> {
    let mut actual = routing_signature(consumer)?;
    actual.tick_interval_ns = expected.tick_interval_ns;
    if actual != *expected {
        return Err(
            "market consumer subscriptions or dependency topology changed after construction"
                .into(),
        );
    }
    Ok(())
}

/// Union this consumer's and all nested indicators' exact requirements.
pub fn effective_subscriptions<C: MarketConsumer + ?Sized>(
    consumer: &mut C,
) -> Result<Vec<Subscription>, String> {
    fn inputs(signature: RoutingSignature, output: &mut Vec<Subscription>) {
        output.extend(signature.own);
        for dependency in signature.dependencies {
            inputs(dependency, output);
        }
    }
    let mut output = Vec::new();
    inputs(routing_signature(consumer)?, &mut output);
    canonical_subscriptions(output)
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
    seen: &mut BTreeSet<(usize, &'static str)>,
    expected: &RoutingSignature,
) -> Result<(), String> {
    validate_routing(consumer, expected)?;
    if !seen.insert(consumer.consumer_identity()) {
        return Ok(());
    }
    for (dependency, signature) in consumer
        .indicators()
        .into_iter()
        .zip(&expected.dependencies)
    {
        let first_visit = !seen.contains(&dependency.consumer_identity());
        dependency.sequence_mut().advance(input_time(input))?;
        route(dependency, input, &mut ObservationContext, seen, signature)?;
        if first_visit {
            capture(dependency, input, signature)?;
        }
    }
    validate_routing(consumer, expected)?;
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
    validate_routing(consumer, expected)
}

/// Route a quote dependency-first, never broadcasting unrequested inputs.
pub fn route_quote<C: MarketConsumer + ?Sized>(
    consumer: &mut C,
    event: &QuoteEvent,
    context: &mut C::Context,
) -> Result<(), String> {
    let signature = routing_signature(consumer)?;
    route_quote_with_signature(consumer, event, context, &signature)
}

pub(crate) fn route_quote_with_signature<C: MarketConsumer + ?Sized>(
    consumer: &mut C,
    event: &QuoteEvent,
    context: &mut C::Context,
    signature: &RoutingSignature,
) -> Result<(), String> {
    route(
        consumer,
        &Input::Quote(event),
        context,
        &mut BTreeSet::new(),
        signature,
    )
}
/// Route a trade dependency-first, never broadcasting unrequested inputs.
pub fn route_trade<C: MarketConsumer + ?Sized>(
    consumer: &mut C,
    event: &TradeEvent,
    context: &mut C::Context,
) -> Result<(), String> {
    let signature = routing_signature(consumer)?;
    route_trade_with_signature(consumer, event, context, &signature)
}

pub(crate) fn route_trade_with_signature<C: MarketConsumer + ?Sized>(
    consumer: &mut C,
    event: &TradeEvent,
    context: &mut C::Context,
    signature: &RoutingSignature,
) -> Result<(), String> {
    route(
        consumer,
        &Input::Trade(event),
        context,
        &mut BTreeSet::new(),
        signature,
    )
}
/// Route an atomic frame dependency-first, filtering interval and symbols.
pub fn route_bar<C: MarketConsumer + ?Sized>(
    consumer: &mut C,
    frame: &BarFrame,
    context: &mut C::Context,
) -> Result<(), String> {
    let signature = routing_signature(consumer)?;
    route_bar_with_signature(consumer, frame, context, &signature)
}

pub(crate) fn route_bar_with_signature<C: MarketConsumer + ?Sized>(
    consumer: &mut C,
    frame: &BarFrame,
    context: &mut C::Context,
    signature: &RoutingSignature,
) -> Result<(), String> {
    route(
        consumer,
        &Input::Bar(frame),
        context,
        &mut BTreeSet::new(),
        signature,
    )
}
