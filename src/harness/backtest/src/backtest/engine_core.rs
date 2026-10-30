//! Owns deterministic replay coordination over injected program and replay capabilities.
//! Subscription planning, venue policy, risk, projection and artifacts have separate owners.
use crate::backtest::config::BacktestConfig;
use crate::backtest::fees::aggregate_fees;
use crate::backtest::fees::fee_schedule_from_config;
use crate::backtest::fees::FeeAssessment;
use crate::backtest::fees::FeeFill;
use crate::backtest::fees::FeeSchedule;
use crate::backtest::fees::DEFAULT_FEE_PRECISION;
use crate::backtest::fixture::DataPreflight;
use crate::backtest::fixture::MarketEvent;
use crate::backtest::fixture::ReplayFixture;
use crate::backtest::fixture::SyntheticDataOptions;
use crate::backtest::manifest::BacktestManifest;
use crate::backtest::portfolio::PortfolioHub;
use harness_trading::program::*;
use harness_trading::replay_errors::BacktestError;
use harness_trading::replay_errors::BacktestStatus;
use num::Decimal;
use std::collections::BTreeMap;
use std::collections::BTreeSet;

use super::event_bus::EngineEvent;
use super::event_bus::EventBus;
use super::execution::Execution;
use super::outcome::BacktestOutcome;
use super::projection::EventProjection;
use super::risk::resolve_risk_rules;
use super::risk::ResolvedRiskRule;
use super::subscriptions::*;

/// Explicit knobs for one native engine run.
#[derive(Clone, Debug)]
pub struct EngineOptions {
    /// String-exact fee precision.
    pub fee_precision: String,
    /// Fee asset class.
    pub asset_class: String,
    /// Scratch resources supplied by application composition.
    pub history: super::history::HistoryResources,
}

impl Default for EngineOptions {
    fn default() -> Self {
        Self {
            fee_precision: DEFAULT_FEE_PRECISION.to_string(),
            asset_class: "equity".to_string(),
            history: super::history::HistoryResources::default(),
        }
    }
}

/// Run one native engine over a fixture and a set of strategy runtimes.
///
/// # Errors
///
/// Returns [`BacktestError`] when the fee schedule or risk rules are invalid,
/// a strategy fails, or replay verification fails.
#[allow(clippy::too_many_arguments)]
pub fn run_native_engine(
    configuration: &BacktestConfig,
    fixture: &ReplayFixture,
    preflight: &DataPreflight,
    mut manifest: BacktestManifest,
    options: &EngineOptions,
    strategies: Vec<StrategyInstance>,
    synthetic_options: &SyntheticDataOptions,
) -> Result<BacktestOutcome, BacktestError> {
    let subscriptions = strategies
        .iter()
        .map(|instance| {
            instance
                .runtime
                .subscriptions()
                .ok_or_else(|| {
                    BacktestError::Configuration("runtime must declare its subscriptions".into())
                })
                .and_then(|inputs| normalize_subscriptions(&inputs))
        })
        .collect::<Result<Vec<_>, _>>()?;
    let requirements =
        normalize_subscriptions(&subscriptions.iter().flatten().cloned().collect::<Vec<_>>())?;
    validate_subscription_echo(configuration, &requirements)?;
    super::preflight::validate_subscription_coverage(fixture, &requirements)?;
    manifest.effective_subscriptions = requirements.clone();
    let execution = if configuration.shell == "study" {
        Vec::new()
    } else {
        normalize_subscriptions(&execution_subscriptions(&requirements))?
    };
    let mut acquired = requirements.clone();
    acquired.extend(execution.iter().cloned());
    manifest.acquired_subscriptions =
        source_subscriptions(&acquired, &configuration.data_source_bar_interval)?;
    manifest.execution_subscriptions = execution;
    let frames =
        assemble_subscription_frames(configuration, fixture, synthetic_options, &requirements)?;
    super::preflight::validate_completed_bar_coverage(&requirements, &frames)?;
    let bar_frame_count = frames.len() as u64;
    let schedule = fee_schedule_from_config(
        &configuration.fees,
        &options.fee_precision,
        "harness-config",
    )?;
    let rules = resolve_risk_rules(configuration)?;
    let mut hub = PortfolioHub::new(&configuration.job_id);
    let mut allocated_cash = Decimal::ZERO;
    for instance in &strategies {
        if instance.instance_id == "__unallocated_cash" {
            return Err(BacktestError::Configuration(
                "reserved unallocated cash book identity".into(),
            ));
        }
        let initial_cash = if configuration.shell == "study" {
            Decimal::ZERO
        } else {
            instance_initial_cash(configuration, &instance.instance_id)?
        };
        if initial_cash.is_negative() {
            return Err(BacktestError::Configuration(
                "capital_fraction must not be negative".into(),
            ));
        }
        allocated_cash = allocated_cash.checked_add(initial_cash).map_err(|error| {
            BacktestError::Arithmetic(format!("allocated capital overflow: {error}"))
        })?;
        hub.create_book(&instance.instance_id, initial_cash);
    }
    let residual = configuration
        .initial_cash_decimal
        .checked_sub(allocated_cash)
        .map_err(|error| {
            BacktestError::Arithmetic(format!("unallocated capital overflow: {error}"))
        })?;
    if residual.is_negative() {
        return Err(BacktestError::Configuration(
            "strategy capital allocations exceed initial capital".into(),
        ));
    }
    if residual.is_positive() {
        hub.create_book("__unallocated_cash", residual);
    }

    let mut strategies = strategies;
    for instance in &mut strategies {
        let initial_cash = instance_initial_cash(configuration, &instance.instance_id)?;
        let start = StrategyStart {
            instance_id: instance.instance_id.clone(),
            account_id: configuration.job_id.clone(),
            symbols: configuration.symbols.clone(),
            seed: configuration.seed,
            initial_cash,
            start_ns: configuration.start_ns,
        };
        instance.runtime.on_start(&start)?;
    }

    let mut bus = EventBus::new();
    bus.frame_replay = !frames.is_empty();
    let mut execution = Execution::new(fixture.sessions.clone());
    let mut prices: BTreeMap<String, Decimal> = BTreeMap::new();
    let mut fills: Vec<EngineFill> = Vec::new();
    let mut valuations = super::valuation::ValuationTracker::new(
        configuration.start_ns,
        configuration.initial_cash_decimal,
        options.history.clone(),
    )?;
    let mut assessments: Vec<FeeAssessment> = Vec::new();
    let mut halted_strategies = BTreeSet::new();
    let mut halt_reason = None;
    let mut market_event_count = 0u64;
    let mut observed: BTreeMap<String, [usize; 3]> = BTreeMap::new();
    let mut observed_days = std::collections::BTreeSet::new();
    let mut last_observed_day = None;
    let projection = EventProjection::new(synthetic_options);
    let mut input = fixture.open_events()?;
    let mut frames = frames.into_iter().peekable();
    let mut next_tick = None;
    let mut exhausted = false;
    let frame_replay = bus.frame_replay;
    let external_key = |event: &EngineEvent| {
        (
            event.timestamp_ns(),
            if frame_replay {
                match event {
                    EngineEvent::Market(_) => 0,
                    EngineEvent::BarFrame(_) => 1,
                    _ => 2,
                }
            } else {
                0
            },
            event.sequence(),
            event.class_priority(),
        )
    };
    loop {
        while next_tick.is_none() && !exhausted {
            match input.next() {
                Some(event) => {
                    let event = event?;
                    market_event_count += 1;
                    let kind = event.kind();
                    if let Some(counts) = observed.get_mut(event.symbol()) {
                        counts[kind] += 1;
                    } else {
                        let mut counts = [0; 3];
                        counts[kind] = 1;
                        observed.insert(event.symbol().to_string(), counts);
                    }
                    let day = event.timestamp_ns().div_euclid(86_400_000_000_000);
                    if last_observed_day != Some(day) {
                        observed_days.insert(day);
                        last_observed_day = Some(day);
                    }
                    if event.timestamp_ns() < configuration.start_ns
                        || event.timestamp_ns() >= configuration.end_ns
                    {
                        return Err(BacktestError::DataPreflight(
                            "stream event outside accepted window".into(),
                        ));
                    }
                    if !matches!(event, MarketEvent::Bar { .. }) {
                        next_tick = Some(EngineEvent::Market(projection.project(event)?));
                    }
                }
                None => exhausted = true,
            }
        }
        if market_event_count.is_multiple_of(65536) {
            if let Some(report) = fixture.stream.as_ref().and_then(|s| s.reporter.as_ref()) {
                let mut progress = harness_contracts::progress::ExecutionProgress::stage(
                    if configuration.shell == "study" {
                        harness_contracts::progress::ExecutionStage::RunningStudy
                    } else {
                        harness_contracts::progress::ExecutionStage::RunningBacktest
                    },
                );
                progress.rows = market_event_count;
                report(progress).map_err(BacktestError::DataPreflight)?;
            }
        }
        let use_frame = frames.peek().is_some_and(|frame| {
            next_tick.as_ref().is_none_or(|tick| {
                (
                    frame.available_at_ns,
                    if frame_replay { 1 } else { 0 },
                    0,
                    9,
                ) < external_key(tick)
            })
        });
        let incoming = if use_frame {
            frames.next().map(EngineEvent::BarFrame)
        } else {
            next_tick.take()
        };
        let boundary = incoming.as_ref().map(&external_key);
        while bus.peek().is_some_and(|queued| {
            boundary.is_none_or(|key| {
                (
                    queued.event.timestamp_ns(),
                    queued.phase,
                    queued.event.sequence(),
                    queued.event.class_priority(),
                ) < key
            })
        }) {
            let dispatched = bus.dispatch_next().expect("nonempty event bus");
            let dispatch_result = dispatch_event(
                &dispatched,
                configuration,
                options,
                &schedule,
                &rules,
                &mut bus,
                &mut execution,
                &mut hub,
                &mut prices,
                &mut fills,
                &mut assessments,
                &mut strategies,
                &subscriptions,
                &mut halted_strategies,
            );
            match dispatch_result {
                Err(BacktestError::StrategyHalt(instance)) => {
                    halted_strategies.insert(instance);
                }
                Err(BacktestError::BacktestAbort(reason)) => {
                    halt_reason = Some(reason);
                    break;
                }
                Err(error) => return Err(error),
                Ok(()) => {}
            }
            match &dispatched {
                EngineEvent::Market(market) => {
                    valuations.observe(
                        dispatched.timestamp_ns(),
                        [(market.symbol(), prices[market.symbol()])],
                        &fills,
                    )?;
                }
                EngineEvent::BarFrame(frame) => {
                    valuations.observe(
                        dispatched.timestamp_ns(),
                        frame
                            .bars_by_symbol
                            .iter()
                            .map(|(symbol, bar)| (symbol.as_str(), bar.close)),
                        &fills,
                    )?;
                }
                EngineEvent::Fill(_) => {
                    valuations.observe(dispatched.timestamp_ns(), [], &fills)?;
                }
                EngineEvent::Report(_) => {}
            }
        }
        if halt_reason.is_some() {
            break;
        }
        match incoming {
            Some(event) => bus.schedule(event),
            None => break,
        }
    }

    let mut stopped = Vec::new();
    let mut teardown_errors = Vec::new();
    let mut custom_metrics = BTreeMap::new();
    let mut exploratory_outputs = BTreeMap::new();
    let mut metric_owners = BTreeMap::new();
    execution.venue.expire(configuration.end_ns);
    for instance in &mut strategies {
        if instance.runtime.wants_execution_state() {
            instance
                .runtime
                .update_execution_state(&execution.snapshot(
                    &instance.instance_id,
                    &hub,
                    configuration.end_ns,
                )?)?;
        }
        if let Err(error) = instance.runtime.on_stop() {
            teardown_errors.push(error.to_string());
        }
        let mut outputs = instance.runtime.published_outputs();
        for output in outputs.values_mut() {
            if output.get("kind").and_then(serde_json::Value::as_str) == Some("fitted_state") {
                if let Some(record) = output.as_object_mut() {
                    record.insert(
                        "training_job_id".into(),
                        serde_json::Value::String(configuration.job_id.clone()),
                    );
                }
            }
        }
        exploratory_outputs.insert(instance.instance_id.clone(), serde_json::json!(outputs));
        for (name, value) in instance.runtime.published_metrics() {
            if let Some(owner) = metric_owners.get(&name) {
                teardown_errors.push(format!(
                    "custom metric {name:?} was published by both {owner:?} and {:?}",
                    instance.instance_id
                ));
                custom_metrics.remove(&name);
                continue;
            }
            metric_owners.insert(name.clone(), instance.instance_id.clone());
            custom_metrics.insert(name, value);
        }
        stopped.push(instance.instance_id.clone());
    }
    if !teardown_errors.is_empty() {
        return Err(BacktestError::StrategyExecution(teardown_errors.join("; ")));
    }

    let order_count: u64 = execution.counts.values().sum();
    let orders = execution.orders();
    if halt_reason.is_none() {
        verify_replay(fixture, preflight, market_event_count)?;
    }
    for input in &requirements {
        if halt_reason.is_some() {
            break;
        }
        for symbol in &input.symbols {
            let kind = match input.data_type.as_str() {
                "quote" => 0,
                "trade" => 1,
                _ => 2,
            };
            if observed.get(symbol).is_none_or(|counts| counts[kind] == 0) {
                return Err(BacktestError::DataPreflight(format!(
                    "replay has no requested {}/{symbol} events inside the window",
                    input.data_type
                )));
            }
        }
    }
    let mut actual_preflight = preflight.clone();
    actual_preflight.event_count = market_event_count as usize;
    actual_preflight.quote_count = observed.values().map(|n| n[0]).sum();
    actual_preflight.trade_count = observed.values().map(|n| n[1]).sum();
    actual_preflight.bar_count = observed.values().map(|n| n[2]).sum();
    actual_preflight.session_count = observed_days.len();
    let fee_totals = aggregate_fees(&assessments)?;
    if let Some(report) = fixture.stream.as_ref().and_then(|s| s.reporter.as_ref()) {
        let mut progress = harness_contracts::progress::ExecutionProgress::stage(
            if configuration.shell == "study" {
                harness_contracts::progress::ExecutionStage::RunningStudy
            } else {
                harness_contracts::progress::ExecutionStage::RunningBacktest
            },
        );
        progress.rows = market_event_count;
        progress.completed = true;
        report(progress).map_err(BacktestError::DataPreflight)?;
    }
    Ok(BacktestOutcome {
        manifest,
        status: if halt_reason.is_some() {
            BacktestStatus::Aborted
        } else {
            BacktestStatus::Completed
        },
        reason: halt_reason,
        preflight: actual_preflight,
        dispatch_hash: bus.dispatch_hash(),
        dispatched_count: bus.dispatched_count(),
        market_event_count,
        bar_frame_count,
        order_count,
        fills,
        valuations: valuations.finish()?,
        fee_assessments: assessments,
        fee_totals,
        portfolio: hub.snapshot(),
        risk_decisions: execution.risk_decisions,
        orders,
        halted_strategies: halted_strategies.into_iter().collect(),
        stopped_strategies: stopped,
        teardown_errors,
        lookahead_violations: Vec::new(),
        custom_metrics,
        exploratory_outputs,
    })
}

#[allow(clippy::too_many_arguments)]
fn dispatch_event(
    event: &EngineEvent,
    configuration: &BacktestConfig,
    options: &EngineOptions,
    schedule: &FeeSchedule,
    rules: &[ResolvedRiskRule],
    bus: &mut EventBus,
    execution: &mut Execution,
    hub: &mut PortfolioHub,
    prices: &mut BTreeMap<String, Decimal>,
    fills: &mut Vec<EngineFill>,
    assessments: &mut Vec<FeeAssessment>,
    strategies: &mut [StrategyInstance],
    subscriptions: &[Vec<MarketSubscription>],
    halted_strategies: &mut BTreeSet<String>,
) -> Result<(), BacktestError> {
    match event {
        EngineEvent::BarFrame(frame) => {
            execution.venue.expire(frame.available_at_ns);
            for bar in frame.bars_by_symbol.values() {
                prices.insert(bar.symbol.clone(), bar.close);
                hub.mark(&bar.symbol, bar.close);
            }
            for (instance, inputs) in strategies.iter_mut().zip(subscriptions) {
                if halted_strategies.contains(&instance.instance_id) {
                    continue;
                }
                let Some(input) = inputs.iter().find(|s| {
                    s.data_type == "bars" && s.interval.as_deref() == Some(frame.interval.as_str())
                }) else {
                    continue;
                };
                let mut routed = frame.clone();
                routed
                    .bars_by_symbol
                    .retain(|symbol, _| input.symbols.contains(symbol));
                routed.missing_symbols = input
                    .symbols
                    .iter()
                    .filter(|s| !routed.bars_by_symbol.contains_key(*s))
                    .cloned()
                    .collect();
                if instance.runtime.wants_execution_state() {
                    instance
                        .runtime
                        .update_execution_state(&execution.snapshot(
                            &instance.instance_id,
                            hub,
                            frame.available_at_ns,
                        )?)?;
                }
                let orders = instance.runtime.on_frame_actions(&routed)?;
                if configuration.shell == "study" && !orders.is_empty() {
                    return Err(BacktestError::StrategyExecution(
                        "studies cannot submit orders".into(),
                    ));
                }
                execution.actions(
                    orders,
                    &instance.instance_id,
                    configuration,
                    rules,
                    frame.available_at_ns,
                    bus,
                    hub,
                    prices,
                    halted_strategies,
                )?;
            }
        }
        EngineEvent::Market(market) => {
            let clock_ns = market.timestamp_ns();
            if configuration.shell != "study" {
                execution.venue.expire(clock_ns);
                execution.venue.on_market(market, clock_ns, bus)?;
            }
            let mark = market.mark_price();
            if let Some(price) = prices.get_mut(market.symbol()) {
                *price = mark;
            } else {
                prices.insert(market.symbol().to_string(), mark);
            }
            hub.mark(market.symbol(), mark);
            for (instance, inputs) in strategies.iter_mut().zip(subscriptions) {
                if halted_strategies.contains(&instance.instance_id) {
                    continue;
                }
                let kind = market.data_type();
                if !inputs
                    .iter()
                    .any(|s| s.data_type == kind && s.symbols.iter().any(|s| s == market.symbol()))
                {
                    continue;
                }
                // Individual bars are assembled before dispatch; tick callbacks
                // receive only the exact declared symbol/type union.
                if instance.runtime.wants_execution_state() {
                    instance
                        .runtime
                        .update_execution_state(&execution.snapshot(
                            &instance.instance_id,
                            hub,
                            clock_ns,
                        )?)?;
                }
                let orders = instance.runtime.on_market_actions(market)?;
                if configuration.shell == "study" && !orders.is_empty() {
                    return Err(BacktestError::StrategyExecution(
                        "studies cannot submit orders".into(),
                    ));
                }
                execution.actions(
                    orders,
                    &instance.instance_id,
                    configuration,
                    rules,
                    clock_ns,
                    bus,
                    hub,
                    prices,
                    halted_strategies,
                )?;
            }
        }
        EngineEvent::Report(report) => {
            execution.venue.on_report(report);
        }
        EngineEvent::Fill(fill) => {
            execution.venue.on_fill(fill);
            let owner = execution.owners.get(&fill.order_id).cloned();
            let strategy_id = owner.clone().unwrap_or_else(|| "unattributed".to_string());
            let fee_fill = FeeFill {
                fill_id: &fill.fill_id,
                side: &fill.side,
                quantity: fill.quantity,
                price: fill.price,
                liquidity: Some(&fill.liquidity),
            };
            let assessment = schedule.calculate(
                &fee_fill,
                &configuration.job_id,
                &strategy_id,
                &options.asset_class,
            )?;
            let booked = EngineFill {
                fee: assessment.total(),
                ..fill.clone()
            };
            assessments.push(assessment);
            if let Some(owner) = &owner {
                hub.apply_fill(
                    owner,
                    &crate::backtest::portfolio::BookFill {
                        fill_id: booked.fill_id.clone(),
                        side: booked.side.clone(),
                        quantity: booked.quantity,
                        price: booked.price,
                        fee: booked.fee,
                        symbol: booked.symbol.clone(),
                    },
                )?;
            }
            fills.push(booked.clone());
            if let Some(owner) = &owner {
                execution.book_fill(owner, &booked)?;
                if let Some(instance) = strategies
                    .iter_mut()
                    .find(|instance| &instance.instance_id == owner)
                {
                    if instance.runtime.wants_execution_state() {
                        instance
                            .runtime
                            .update_execution_state(&execution.snapshot(
                                owner,
                                hub,
                                booked.timestamp_ns,
                            )?)?;
                    }
                    instance.runtime.on_fill(&booked)?;
                }
                execution.reconcile_owner(
                    owner,
                    configuration,
                    rules,
                    booked.timestamp_ns,
                    bus,
                    hub,
                    prices,
                    halted_strategies,
                )?;
            }
        }
    }
    Ok(())
}

/// The starting cash of one strategy instance: the configured initial cash
/// times the instance's capital fraction.
///
/// # Errors
///
/// Returns [`BacktestError::Configuration`] when the fraction is malformed and
/// [`BacktestError::Arithmetic`] when the product cannot be represented exactly.
/// Neither case falls back to full cash.
fn instance_initial_cash(
    configuration: &BacktestConfig,
    instance_id: &str,
) -> Result<Decimal, BacktestError> {
    let fraction = capital_fraction(configuration, instance_id)?;
    configuration
        .initial_cash_decimal
        .checked_mul(fraction)
        .map_err(|error| {
            BacktestError::Arithmetic(format!(
                "initial cash times capital fraction for strategy instance {instance_id} \
                 cannot be represented exactly: {error}"
            ))
        })
}

/// The capital fraction one strategy instance is configured with.
///
/// An instance the configuration does not declare takes the default fraction of
/// `1.0`, the same default the YAML parser applies when `capital_fraction` is
/// omitted. A declared fraction that is not an exact decimal is an error.
///
/// # Errors
///
/// Returns [`BacktestError::Configuration`] when the declared fraction is not an
/// exact decimal.
fn capital_fraction(
    configuration: &BacktestConfig,
    instance_id: &str,
) -> Result<Decimal, BacktestError> {
    let Some(instance) = configuration
        .strategy_instances
        .iter()
        .find(|instance| instance.instance_id == instance_id)
    else {
        return Ok(Decimal::ONE);
    };
    instance.capital_fraction.parse::<Decimal>().map_err(|error| {
        BacktestError::Configuration(format!(
            "capital_fraction {:?} for strategy instance {instance_id} is not an exact decimal: {error}",
            instance.capital_fraction
        ))
    })
}

fn verify_replay(
    fixture: &ReplayFixture,
    preflight: &DataPreflight,
    market_event_count: u64,
) -> Result<(), BacktestError> {
    let expected = fixture
        .stream
        .as_ref()
        .map_or(Some(preflight.event_count), |s| s.expected_count);
    if market_event_count == 0 || expected.is_some_and(|count| market_event_count != count as u64) {
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
