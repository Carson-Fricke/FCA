//! Owns trader lifecycle decisions over explicit clock, feed, snapshot and journal ports.
//! Configuration parsing, concrete storage and process startup have separate owners.
use std::rc::Rc;

use crate::runtime::paper::PaperOptions;
use crate::runtime::paper::PaperRuntimeShell;
use crate::runtime::paper::PaperShell;
use crate::runtime::reconciler::reconcile_cash;
use crate::runtime::reconciler::reconcile_positions;
use crate::runtime::reconciler::BrokerPosition;
use crate::runtime::reconciler::ExpectedPosition;
use crate::runtime::reconciler::ReconciliationTolerances;
use crate::runtime::state_store::RuntimeStateStore;
use crate::runtime::state_store::SnapshotTrigger;

use harness_trading::events::MarketEvent;
use harness_trading::risk::kill_switch::KillSwitch;
pub mod models;
use models::*;
pub mod clock;
use clock::*;
pub mod feed;
use feed::*;
pub mod configuration;
use configuration::*;
pub mod journal;
pub mod snapshots;
use journal::*;
pub mod process;
/// Actor recorded on every supervisor-driven kill-switch action.
const KILL_SWITCH_ACTOR: &str = "trader-supervisor";

enum SessionEnd {
    AtClose,
    Stopped(String),
}

/// Drive one supervised paper or shadow session to a terminal outcome.
pub struct TraderSupervisor {
    config: TraderConfig,
    job_id: String,
    settings: SupervisorSettings,
    session: Option<SessionWindow>,
    clock: Rc<dyn SupervisionClock>,
    feed: SupervisedFeed,
    kill_switch: KillSwitch,
    poller: Option<Box<dyn KillSwitchPoller>>,
    state_store: RuntimeStateStore,
    journal: SupervisionJournal,
    phase: SupervisionPhase,
    snapshot_sequence: u64,
    market_event_count: u64,
    poll_failures: u32,
    next_heartbeat_ns: i64,
    next_kill_switch_poll_ns: i64,
    next_snapshot_ns: i64,
    next_reconcile_ns: i64,
    market_deadline_ns: i64,
    last_snapshot_id: Option<String>,
    emit: Box<dyn Fn(&str)>,
}

impl TraderSupervisor {
    /// Compose a supervisor from its ports and durable stores.
    ///
    /// # Errors
    ///
    /// Returns [`SupervisionError::Configuration`] when the settings, job
    /// identifiers, or kill switch are invalid.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        config: TraderConfig,
        settings: SupervisorSettings,
        session: Option<SessionWindow>,
        state_store: RuntimeStateStore,
        journal: SupervisionJournal,
        clock: Rc<dyn SupervisionClock>,
        feed: SupervisedFeed,
        kill_switch: KillSwitch,
        poller: Option<Box<dyn KillSwitchPoller>>,
        emit: Box<dyn Fn(&str)>,
    ) -> Result<Self, SupervisionError> {
        settings.validate()?;
        let job_id = match config.job_id.clone() {
            Some(job_id) => job_id,
            None => state_store.job_id().to_string(),
        };
        if state_store.job_id() != job_id {
            return Err(SupervisionError::Configuration(format!(
                "state store job {:?} does not match job {:?}",
                state_store.job_id(),
                job_id
            )));
        }
        if journal.job_id() != job_id {
            return Err(SupervisionError::Configuration(format!(
                "journal job {:?} does not match job {:?}",
                journal.job_id(),
                job_id
            )));
        }
        let mut config = config;
        config.job_id = Some(job_id.clone());
        if let Some(window) = session {
            if window.close_ns <= window.open_ns {
                return Err(SupervisionError::Configuration(
                    "session close must be after open".to_string(),
                ));
            }
        }
        Ok(Self {
            next_heartbeat_ns: clock.now_ns(),
            next_kill_switch_poll_ns: clock.now_ns(),
            next_snapshot_ns: clock.now_ns(),
            next_reconcile_ns: clock.now_ns(),
            market_deadline_ns: 0,
            config,
            job_id,
            settings,
            session,
            clock,
            feed,
            kill_switch,
            poller,
            state_store,
            journal,
            phase: SupervisionPhase::Boot,
            snapshot_sequence: 0,
            market_event_count: 0,
            poll_failures: 0,
            last_snapshot_id: None,
            emit,
        })
    }

    /// The supervised execution job identifier.
    #[must_use]
    pub fn job_id(&self) -> &str {
        &self.job_id
    }

    /// The most recent audited phase.
    #[must_use]
    pub const fn phase(&self) -> SupervisionPhase {
        self.phase
    }

    /// The in-process kill switch.
    #[must_use]
    pub const fn kill_switch(&self) -> &KillSwitch {
        &self.kill_switch
    }

    /// The supervised feed's reconnect and drop counters.
    #[must_use]
    pub const fn feed_counters(&self) -> (u64, u64) {
        (self.feed.reconnects(), self.feed.drops())
    }

    /// Run one supervised boot to its terminal outcome.
    pub fn run(&mut self) -> SupervisorOutcome {
        match self.boot() {
            Ok(outcome) => outcome,
            Err(error) => {
                let restartable = matches!(error, SupervisionError::Restartable(_));
                self.fault_outcome(error, restartable)
            }
        }
    }

    fn boot(&mut self) -> Result<SupervisorOutcome, SupervisionError> {
        self.transition(
            SupervisionPhase::Boot,
            &format!("mode={}", self.config.mode),
            None,
        );
        if self.journal.consecutive_faults() >= self.settings.restart_budget {
            return Ok(self.outcome(
                TraderExitCode::CrashLoop,
                SupervisionPhase::CrashLoop,
                Some("restart budget exhausted; refusing to boot".to_string()),
                OutcomeKind::Fault,
            ));
        }
        if self.gate_blocks_boot()? {
            return Ok(self.outcome(
                TraderExitCode::KillSwitchHalted,
                SupervisionPhase::Halted,
                Some("kill switch engaged at boot".to_string()),
                OutcomeKind::KillSwitch,
            ));
        }
        let Some(session) = self.session else {
            return Ok(self.outcome(
                TraderExitCode::Success,
                SupervisionPhase::NoSession,
                Some("no market session is scheduled for the boot time".to_string()),
                OutcomeKind::Clean,
            ));
        };
        self.transition(
            SupervisionPhase::Restore,
            &format!("boot-policy {}", self.settings.boot_policy.value()),
            None,
        );
        let resumed = self.load_snapshot(session)?;
        self.transition(
            SupervisionPhase::Connect,
            "connecting market-data boundary",
            None,
        );
        self.feed.connect()?;
        let options = self.paper_options(&self.job_id);
        let mut shell = match resumed {
            Some(snapshot) => {
                let restored = self.state_store.restore_shell(&snapshot, options.clone())?;
                self.last_snapshot_id = Some(snapshot.snapshot_id());
                restored
            }
            None => PaperRuntimeShell::new(options.clone(), self.books()),
        };
        self.journal_status(
            SupervisionPhase::Running,
            "session pump starting",
            &shell,
            None,
        );
        self.arm_cadences(session);
        let end = self.pump(&mut shell)?;
        self.finish(&mut shell, end)
    }

    fn books(&self) -> Vec<(String, String)> {
        self.config
            .strategy_instance_ids
            .iter()
            .map(|instance_id| (instance_id.clone(), self.settings.initial_cash.to_string()))
            .collect()
    }

    fn paper_options(&self, job_id: &str) -> PaperOptions {
        let mut options = PaperOptions::new(job_id);
        options.initial_cash = self.settings.initial_cash.to_string();
        options.session_policy = self.config.session_policy();
        options
    }

    fn gate_blocks_boot(&mut self) -> Result<bool, SupervisionError> {
        self.transition(SupervisionPhase::Gate, "kill-switch gate", None);
        let Some(poller) = self.poller.as_mut() else {
            return Ok(false);
        };
        let observation = poller.poll().map_err(|error| {
            SupervisionError::Restartable(format!("kill-switch gate poll failed: {error}"))
        })?;
        if !observation.engaged {
            return Ok(false);
        }
        let reason = observation
            .reason
            .unwrap_or_else(|| "operator engaged".to_string());
        self.kill_switch
            .trip(Some(&reason), Some(KILL_SWITCH_ACTOR))?;
        self.transition(
            SupervisionPhase::Halted,
            &format!("kill-switch gate: {reason}"),
            None,
        );
        Ok(true)
    }

    fn load_snapshot(
        &mut self,
        session: SessionWindow,
    ) -> Result<Option<crate::runtime::state_store::RuntimeSnapshot>, SupervisionError> {
        let snapshot = match self.state_store.load() {
            Ok(snapshot) => snapshot,
            Err(crate::runtime::state_store::StateStoreError::Missing(_)) => return Ok(None),
            Err(error) => {
                return Err(SupervisionError::Restartable(format!(
                    "snapshot load failed: {error}"
                )))
            }
        };
        if self.config.mode != "paper" {
            return Ok(None);
        }
        if snapshot.trigger == SnapshotTrigger::Stop {
            // A clean stop snapshot is not resumed.
            return Ok(None);
        }
        if snapshot.job_id != self.job_id {
            return Err(SupervisionError::Configuration(format!(
                "persisted snapshot job {:?} does not match job {:?}",
                snapshot.job_id, self.job_id
            )));
        }
        if session.close_ns <= session.open_ns {
            let snapshot_id = self.last_snapshot_id.clone();
            self.transition(
                SupervisionPhase::Restore,
                "snapshot session differs from the scheduled session",
                snapshot_id.as_deref(),
            );
            return Ok(None);
        }
        self.transition(
            SupervisionPhase::Restore,
            "snapshot session matches; resuming",
            Some(&snapshot.snapshot_id()),
        );
        if self.settings.boot_policy == BootPolicy::HaltOnly {
            return Err(SupervisionError::Fault(
                "boot policy halt_only refuses to resume".to_string(),
            ));
        }
        Ok(Some(snapshot))
    }

    fn arm_cadences(&mut self, session: SessionWindow) {
        let now = self.clock.now_ns();
        self.market_deadline_ns = session.open_ns.max(now) + self.settings.market_wait_ns;
        self.next_heartbeat_ns = now + self.settings.heartbeat_cadence_ns;
        self.next_kill_switch_poll_ns = now + self.settings.kill_switch_poll_ns;
        self.next_snapshot_ns = now + self.settings.snapshot_cadence_ns;
        self.next_reconcile_ns = now + self.settings.reconcile_cadence_ns;
    }

    fn pump(&mut self, shell: &mut PaperRuntimeShell) -> Result<SessionEnd, SupervisionError> {
        loop {
            match self.feed.next_event()? {
                Some(event) => {
                    self.apply_event(shell, &event);
                    if let Some(reason) = self.housekeeping(shell)? {
                        return Ok(SessionEnd::Stopped(reason));
                    }
                }
                None => return Ok(SessionEnd::AtClose),
            }
        }
    }

    fn apply_event(&mut self, shell: &mut PaperRuntimeShell, event: &MarketEvent) {
        let price = match event {
            MarketEvent::Quote(quote) => quote.ask_price,
            MarketEvent::Trade(trade) => trade.price,
        };
        shell.set_price(event.symbol().as_str().to_string(), price.to_string());
        let session = shell.session_mut();
        session.market_event_count += 1;
        session.dispatched_count += 1;
        self.market_event_count += 1;
    }

    fn housekeeping(
        &mut self,
        shell: &mut PaperRuntimeShell,
    ) -> Result<Option<String>, SupervisionError> {
        let now = self.clock.now_ns();
        if shell.result().market_event_count == 0 && now >= self.market_deadline_ns {
            return Err(SupervisionError::Restartable(format!(
                "no market events by the market-wait deadline ({} ns)",
                self.market_deadline_ns
            )));
        }
        if now >= self.next_kill_switch_poll_ns {
            self.next_kill_switch_poll_ns = now + self.settings.kill_switch_poll_ns;
            if let Some(reason) = self.poll_kill_switch()? {
                return Ok(Some(reason));
            }
        }
        if now >= self.next_heartbeat_ns {
            self.next_heartbeat_ns = now + self.settings.heartbeat_cadence_ns;
            self.journal_status(
                SupervisionPhase::Running,
                "heartbeat",
                shell,
                Some("heartbeat"),
            );
        }
        if now >= self.next_snapshot_ns {
            self.next_snapshot_ns = now + self.settings.snapshot_cadence_ns;
            self.capture(shell, SnapshotTrigger::Periodic)?;
        }
        if now >= self.next_reconcile_ns {
            self.next_reconcile_ns = now + self.settings.reconcile_cadence_ns;
            self.periodic_reconcile(shell)?;
        }
        Ok(None)
    }

    fn poll_kill_switch(&mut self) -> Result<Option<String>, SupervisionError> {
        let Some(poller) = self.poller.as_mut() else {
            return Ok(None);
        };
        let observation = match poller.poll() {
            Ok(observation) => observation,
            Err(error) => {
                self.poll_failures += 1;
                if self.poll_failures > self.settings.kill_switch_stale {
                    return Err(SupervisionError::Restartable(format!(
                        "kill-switch poll failed {} consecutive times: {error}",
                        self.poll_failures
                    )));
                }
                return Ok(None);
            }
        };
        self.poll_failures = 0;
        if !observation.engaged {
            return Ok(None);
        }
        let reason = observation
            .reason
            .unwrap_or_else(|| "operator engaged".to_string());
        self.kill_switch
            .trip(Some(&reason), Some(KILL_SWITCH_ACTOR))?;
        let detail = format!("kill-switch engaged: {reason}");
        let snapshot_id = self.last_snapshot_id.clone();
        self.transition(SupervisionPhase::Running, &detail, snapshot_id.as_deref());
        Ok(Some(detail))
    }

    fn capture(
        &mut self,
        shell: &PaperRuntimeShell,
        trigger: SnapshotTrigger,
    ) -> Result<(), SupervisionError> {
        let options = self.paper_options(&self.job_id);
        let snapshot = self.state_store.capture(
            shell,
            &options,
            &self.config.strategy_instance_ids,
            trigger,
            self.snapshot_sequence,
            None,
            &[],
        )?;
        self.snapshot_sequence += 1;
        let stored = self.state_store.save(&snapshot)?;
        self.last_snapshot_id = Some(stored.snapshot_id.clone());
        self.journal_status(
            SupervisionPhase::Running,
            &format!("{} snapshot saved", trigger.value()),
            shell,
            Some(&stored.snapshot_id),
        );
        Ok(())
    }

    fn periodic_reconcile(&mut self, shell: &PaperRuntimeShell) -> Result<(), SupervisionError> {
        let tolerances = ReconciliationTolerances::default();
        let expected: Vec<ExpectedPosition> = Vec::new();
        let actual: Vec<BrokerPosition> = Vec::new();
        let position_deltas =
            reconcile_positions(&expected, &actual, tolerances).map_err(|error| {
                SupervisionError::Fault(format!("position reconciliation failed: {error}"))
            })?;
        let expected_cash = self.settings.initial_cash.to_string();
        let actual_cash = shell
            .books()
            .first()
            .map_or_else(|| expected_cash.clone(), |book| book.cash.clone());
        let cash_delta =
            reconcile_cash(&expected_cash, &actual_cash, tolerances).map_err(|error| {
                SupervisionError::Fault(format!("cash reconciliation failed: {error}"))
            })?;
        if !position_deltas.is_empty() || cash_delta.is_some() {
            return Err(SupervisionError::Restartable(format!(
                "reconciliation diverged (positions={}, cash={:?})",
                position_deltas.len(),
                cash_delta
            )));
        }
        let snapshot_id = self.last_snapshot_id.clone();
        self.transition(
            SupervisionPhase::Running,
            "periodic reconciliation matched",
            snapshot_id.as_deref(),
        );
        Ok(())
    }

    fn finish(
        &mut self,
        shell: &mut PaperRuntimeShell,
        end: SessionEnd,
    ) -> Result<SupervisorOutcome, SupervisionError> {
        match end {
            SessionEnd::AtClose => {
                self.capture(shell, SnapshotTrigger::SessionEnd)?;
                self.transition(SupervisionPhase::Teardown, "session closed", None);
                Ok(self.outcome(
                    TraderExitCode::Success,
                    SupervisionPhase::AtClose,
                    None,
                    OutcomeKind::Clean,
                ))
            }
            SessionEnd::Stopped(reason) => {
                self.capture(shell, SnapshotTrigger::Stop)?;
                if reason.starts_with("kill-switch") {
                    self.transition(SupervisionPhase::Halted, &reason, None);
                    Ok(self.outcome(
                        TraderExitCode::KillSwitchHalted,
                        SupervisionPhase::Halted,
                        Some(reason),
                        OutcomeKind::KillSwitch,
                    ))
                } else if reason.starts_with("reconciliation") {
                    Ok(self.fault_outcome(SupervisionError::Fault(reason), false))
                } else {
                    self.transition(SupervisionPhase::Stopped, &reason, None);
                    Ok(self.outcome(
                        TraderExitCode::Success,
                        SupervisionPhase::Stopped,
                        Some(reason),
                        OutcomeKind::Clean,
                    ))
                }
            }
        }
    }

    fn fault_outcome(&mut self, error: SupervisionError, restartable: bool) -> SupervisorOutcome {
        let detail = error.to_string();
        self.transition(SupervisionPhase::Faulted, &detail, None);
        let (exit_code, kind) = if restartable {
            (TraderExitCode::RestartableFault, OutcomeKind::Fault)
        } else {
            (TraderExitCode::Failed, OutcomeKind::Fault)
        };
        self.outcome(exit_code, self.phase, Some(detail), kind)
    }

    fn outcome(
        &mut self,
        exit_code: TraderExitCode,
        phase: SupervisionPhase,
        detail: Option<String>,
        kind: OutcomeKind,
    ) -> SupervisorOutcome {
        self.phase = phase;
        let market_event_count = self.market_event_count;
        self.journal.record_status(
            phase,
            detail.as_deref().unwrap_or(phase.value()),
            market_event_count,
            self.last_snapshot_id.as_deref(),
            false,
        );
        self.journal
            .record_boot_outcome(kind, detail.as_deref().unwrap_or(phase.value()));
        (self.emit)(&format!(
            "supervision job={} phase={} exit={}",
            self.job_id,
            phase.value(),
            exit_code.code()
        ));
        SupervisorOutcome {
            job_id: self.job_id.clone(),
            exit_code,
            phase,
            detail,
            market_event_count,
            snapshot_id: self.last_snapshot_id.clone(),
        }
    }

    fn transition(&mut self, phase: SupervisionPhase, detail: &str, snapshot_id: Option<&str>) {
        self.phase = phase;
        self.journal.record_status(
            phase,
            detail,
            self.market_event_count,
            snapshot_id.or(self.last_snapshot_id.as_deref()),
            false,
        );
    }

    fn journal_status(
        &mut self,
        phase: SupervisionPhase,
        detail: &str,
        shell: &PaperRuntimeShell,
        snapshot_id: Option<&str>,
    ) {
        self.journal.record_status(
            phase,
            detail,
            shell.result().market_event_count,
            snapshot_id.or(self.last_snapshot_id.as_deref()),
            detail == "heartbeat",
        );
        if detail == "heartbeat" {
            (self.emit)(&format!(
                "heartbeat job={} phase={} at={}",
                self.job_id,
                phase.value(),
                self.clock.now_ns()
            ));
        }
    }
}

/// Eight hours in nanoseconds: the default supervised paper session window.
const DEFAULT_SESSION_NS: i64 = 8 * 3_600_000_000_000;
