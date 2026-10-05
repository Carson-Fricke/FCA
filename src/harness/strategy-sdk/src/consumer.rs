//! Shared subscription declarations and dependency-first market routing.

pub use harness_contracts::{canonical_subscriptions, Subscription};
use std::collections::BTreeSet;

use crate::{BarFrame, Indicator, QuoteEvent, TradeEvent};

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
        dependencies.push(collect(dependency, visiting)?);
    }
    visiting.remove(&identity);
    Ok(RoutingSignature {
        identity,
        own,
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
    if routing_signature(consumer)? != *expected {
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
        route(dependency, input, &mut ObservationContext, seen, signature)?;
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
