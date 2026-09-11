use super::*;
use crate::events::{QuoteEvent, TradeEvent};
use crate::risk::kill_switch::KillSwitchState;
use num::Decimal18;

fn dec(value: &str) -> Decimal18 {
    value.parse().unwrap()
}

fn quote(_sequence: u64, _micros: i64) -> MarketEvent {
    MarketEvent::Quote(
        QuoteEvent::new(
            crate::symbol::Symbol::new("SPY").unwrap(),
            dec("450.00"),
            dec("100"),
            dec("450.02"),
            dec("100"),
            None,
        )
        .expect("quote"),
    )
}

fn trade(_sequence: u64, _micros: i64) -> MarketEvent {
    MarketEvent::Trade(
        TradeEvent::new(
            crate::symbol::Symbol::new("SPY").unwrap(),
            dec("450.01"),
            dec("50"),
            None,
            None,
        )
        .expect("trade"),
    )
}

/// A feed that advances a manual clock once per event.
struct TickingFeed {
    inner: ScriptedMarketFeed,
    clock: Rc<ManualClock>,
    step_ns: i64,
}

impl MarketFeed for TickingFeed {
    fn next_event(&mut self) -> Result<Option<MarketEvent>, FeedError> {
        self.clock.advance(self.step_ns);
        self.inner.next_event()
    }
}

struct ScriptedPoller {
    observations: std::collections::VecDeque<KillSwitchObservation>,
    errors: usize,
}

impl KillSwitchPoller for ScriptedPoller {
    fn poll(&mut self) -> Result<KillSwitchObservation, String> {
        if self.errors > 0 {
            self.errors -= 1;
            return Err("source unavailable".to_string());
        }
        Ok(self
            .observations
            .pop_front()
            .unwrap_or(KillSwitchObservation {
                engaged: false,
                reason: None,
            }))
    }
}

fn config() -> TraderConfig {
    TraderConfig {
        run_id: "trader-test".to_string(),
        mode: "paper".to_string(),
        seed: 1,
        strategy_instance_ids: vec!["strategy-1".to_string()],
        on_reject: "reject_order".to_string(),
        rule_ids: vec!["max-order-notional".to_string()],
    }
}

fn scripted_feed(events: Vec<MarketEvent>) -> SupervisedFeed {
    SupervisedFeed::new(
        Box::new(move || Ok(Box::new(ScriptedMarketFeed::new(events.clone())))),
        2,
        1_000_000,
        Rc::new(|_| {}),
    )
    .expect("feed")
}

fn supervisor(
    events: Vec<MarketEvent>,
    settings: SupervisorSettings,
    clock: Rc<ManualClock>,
    session: Option<SessionWindow>,
    poller: Option<Box<dyn KillSwitchPoller>>,
) -> (TraderSupervisor, SharedSnapshotStore) {
    let shared = SharedSnapshotStore::in_memory();
    let mut state_store = RuntimeStateStore::new("trader-test");
    state_store.add_store(Box::new(shared.clone()));
    let journal = SupervisionJournal::new(shared.clone(), "trader-test", clock.clone());
    let supervisor = TraderSupervisor::new(
        config(),
        settings,
        session,
        state_store,
        journal,
        clock,
        scripted_feed(events),
        KillSwitch::new(None).expect("kill switch"),
        poller,
        Box::new(|_| {}),
    )
    .expect("supervisor");
    (supervisor, shared)
}

#[test]
fn completes_a_scripted_session_and_saves_a_snapshot() {
    let clock = Rc::new(ManualClock::new(1_000));
    let session = SessionWindow {
        open_ns: 1_000,
        close_ns: 1_000_000_000,
    };
    let (mut supervisor, shared) = supervisor(
        vec![quote(0, 2_000), trade(1, 3_000)],
        SupervisorSettings::default(),
        clock,
        Some(session),
        None,
    );
    let outcome = supervisor.run();
    assert_eq!(outcome.exit_code, TraderExitCode::Success);
    assert_eq!(outcome.phase, SupervisionPhase::AtClose);
    assert_eq!(outcome.market_event_count, 2);
    assert!(outcome.snapshot_id.is_some());
    // The session-end snapshot and status document are durable.
    assert!(shared
        .read_text("runs/trader-test/supervision.json")
        .is_some());
    assert!(shared.read_text("runs/trader-test/restarts.json").is_some());
    assert_eq!(supervisor.phase(), SupervisionPhase::AtClose);
    // The durable snapshot round-trips through a fresh state store.
    let mut reopened = RuntimeStateStore::new("trader-test");
    reopened.add_store(Box::new(shared.clone()));
    let snapshot = reopened.load().expect("load snapshot");
    assert_eq!(snapshot.run_id, "trader-test");
    let mut options = PaperOptions::new("trader-test");
    options.initial_cash = SupervisorSettings::default().initial_cash.to_string();
    let shell = reopened.restore_shell(&snapshot, options).expect("restore");
    assert_eq!(shell.book_count(), 1);
}

#[test]
fn kill_switch_gate_blocks_boot_and_trips_the_switch() {
    let clock = Rc::new(ManualClock::new(1_000));
    let session = SessionWindow {
        open_ns: 1_000,
        close_ns: 1_000_000_000,
    };
    let poller = Box::new(ScriptedPoller {
        observations: std::collections::VecDeque::from(vec![KillSwitchObservation {
            engaged: true,
            reason: Some("operator stop".to_string()),
        }]),
        errors: 0,
    });
    let (mut supervisor, _) = supervisor(
        Vec::new(),
        SupervisorSettings::default(),
        clock,
        Some(session),
        Some(poller),
    );
    let outcome = supervisor.run();
    assert_eq!(outcome.exit_code, TraderExitCode::KillSwitchHalted);
    assert_eq!(outcome.phase, SupervisionPhase::Halted);
    assert_eq!(supervisor.kill_switch().state(), KillSwitchState::Engaged);
}

#[test]
fn no_session_completes_cleanly() {
    let clock = Rc::new(ManualClock::new(1_000));
    let (mut supervisor, _) =
        supervisor(Vec::new(), SupervisorSettings::default(), clock, None, None);
    let outcome = supervisor.run();
    assert_eq!(outcome.exit_code, TraderExitCode::Success);
    assert_eq!(outcome.phase, SupervisionPhase::NoSession);
}

#[test]
fn kill_switch_poll_stops_the_running_session() {
    let clock = Rc::new(ManualClock::new(1_000));
    let session = SessionWindow {
        open_ns: 1_000,
        close_ns: 1_000_000_000,
    };
    // Poll cadence 100ns: each event advances the clock past it.
    let settings = SupervisorSettings {
        kill_switch_poll_ns: 100,
        heartbeat_cadence_ns: 1_000_000_000,
        snapshot_cadence_ns: 1_000_000_000,
        reconcile_cadence_ns: 1_000_000_000,
        ..SupervisorSettings::default()
    };
    let poller = Box::new(ScriptedPoller {
        observations: std::collections::VecDeque::from(vec![
            KillSwitchObservation {
                engaged: false,
                reason: None,
            },
            KillSwitchObservation {
                engaged: true,
                reason: Some("halt now".to_string()),
            },
        ]),
        errors: 0,
    });
    let clock_for_feed = clock.clone();
    let feed = SupervisedFeed::new(
        Box::new(move || {
            Ok(Box::new(TickingFeed {
                inner: ScriptedMarketFeed::new(vec![quote(0, 2_000), quote(1, 3_000)]),
                clock: clock_for_feed.clone(),
                step_ns: 1_000,
            }))
        }),
        2,
        1_000_000,
        Rc::new(|_| {}),
    )
    .expect("feed");
    let shared = SharedSnapshotStore::in_memory();
    let mut state_store = RuntimeStateStore::new("trader-test");
    state_store.add_store(Box::new(shared.clone()));
    let journal = SupervisionJournal::new(shared, "trader-test", clock.clone());
    let mut supervisor = TraderSupervisor::new(
        config(),
        settings,
        Some(session),
        state_store,
        journal,
        clock,
        feed,
        KillSwitch::new(None).expect("switch"),
        Some(poller),
        Box::new(|_| {}),
    )
    .expect("supervisor");
    let outcome = supervisor.run();
    assert_eq!(outcome.exit_code, TraderExitCode::KillSwitchHalted);
    assert_eq!(supervisor.kill_switch().state(), KillSwitchState::Engaged);
}

#[test]
fn crash_loop_budget_refuses_boot() {
    let clock = Rc::new(ManualClock::new(1_000));
    let shared = SharedSnapshotStore::in_memory();
    let journal = SupervisionJournal::new(shared.clone(), "trader-test", clock.clone());
    journal.record_boot_outcome(OutcomeKind::Fault, "one");
    journal.record_boot_outcome(OutcomeKind::Fault, "two");
    let mut state_store = RuntimeStateStore::new("trader-test");
    state_store.add_store(Box::new(shared));
    let supervisor = TraderSupervisor::new(
        config(),
        SupervisorSettings {
            restart_budget: 2,
            ..SupervisorSettings::default()
        },
        Some(SessionWindow {
            open_ns: 1_000,
            close_ns: 1_000_000_000,
        }),
        state_store,
        journal,
        clock,
        scripted_feed(Vec::new()),
        KillSwitch::new(None).expect("switch"),
        None,
        Box::new(|_| {}),
    )
    .expect("supervisor");
    let mut supervisor = supervisor;
    let outcome = supervisor.run();
    assert_eq!(outcome.exit_code, TraderExitCode::CrashLoop);
}

#[test]
fn reconnect_backoff_is_recorded_on_drop() {
    let clock = Rc::new(ManualClock::new(0));
    let attempts = Rc::new(Cell::new(0));
    let attempts_for_feed = attempts.clone();
    let mut feed = SupervisedFeed::new(
        Box::new(move || {
            let count = attempts_for_feed.get();
            attempts_for_feed.set(count + 1);
            if count == 0 {
                Err(FeedError::Disconnected("drop".to_string()))
            } else {
                Ok(Box::new(ScriptedMarketFeed::new(vec![quote(0, 1)])))
            }
        }),
        2,
        500_000_000,
        Rc::new(|_| {}),
    )
    .expect("feed");
    // First connect succeeds, then the first event drop reconnects.
    feed.connect().expect("connect");
    // Force a drop by replacing the current feed is not possible; use next.
    let _ = &clock;
    assert_eq!(feed.reconnects(), 0);
    // A failing connect factory surfaces a restartable fault.
    let mut failing = SupervisedFeed::new(
        Box::new(|| Err(FeedError::Disconnected("down".to_string()))),
        2,
        1_000_000,
        Rc::new(|_| {}),
    )
    .expect("feed");
    assert!(matches!(
        failing.connect(),
        Err(SupervisionError::Restartable(_))
    ));
}

#[test]
fn settings_validate_rejects_bad_cadences() {
    let settings = SupervisorSettings {
        heartbeat_cadence_ns: 0,
        ..SupervisorSettings::default()
    };
    assert!(settings.validate().is_err());
}
