//! Configured trader supervision runner.
//!
//! This is the deployment lifecycle the deterministic live core does not own:
//! it loads a trader configuration, gates every boot on the operator kill
//! switch, restores interrupted sessions from a durable snapshot, supervises a
//! live market-data feed (reconnect plus backoff), runs continuous
//! reconciliation and periodic snapshots, and tears the session down with a
//! durable, audited snapshot.
//!
//! The kernel is deterministic: every wall-clock value enters through an
//! injected [`SupervisionClock`], every blocking pace through an injected
//! sleeper, and the market-data boundary through a [`MarketFeed`] port. Given
//! the same clock, configuration, ports, and events, the supervision decisions
//! and their audit records are identical. The binary composes the ports; tests
//! inject deterministic fakes and never touch the network.

use std::cell::{Cell, RefCell};
use std::path::PathBuf;
use std::rc::Rc;

use backtest::runtime::paper::{PaperOptions, PaperRuntimeShell, PaperSessionPolicy, PaperShell};
use backtest::runtime::reconciler::{
    reconcile_cash, reconcile_positions, BrokerPosition, ExpectedPosition, ReconciliationTolerances,
};
use backtest::runtime::state_store::{
    InMemorySnapshotStore, RuntimeStateStore, SnapshotObjectStore, SnapshotTrigger,
};
use num::Decimal18;
use serde_json::{json, Value};

use crate::error::LiveError;
use crate::events::MarketEvent;
use crate::risk::kill_switch::KillSwitch;
use crate::venues::transport::RetryConfig;

/// Default supervision run document suffix.
pub const DEFAULT_CONFIG_PATH: &str = "config/harness/trader.yaml";
/// Actor recorded on every supervisor-driven kill-switch action.
const KILL_SWITCH_ACTOR: &str = "trader-supervisor";
/// Maximum restart-ledger history entries retained.
const LEDGER_HISTORY_LIMIT: usize = 64;

/// Process exit codes the platform observes on every supervised boot.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TraderExitCode {
    /// A deliberate, operator-expected completion.
    Success = 0,
    /// A terminal, non-restartable failure.
    Failed = 1,
    /// A usage or configuration error.
    Usage = 2,
    /// The operator kill switch halted the boot.
    KillSwitchHalted = 41,
    /// A boot policy halted the session.
    PolicyHalted = 42,
    /// A transient fault the platform may recover by restart.
    RestartableFault = 75,
    /// The restart budget is spent; refusing to boot.
    CrashLoop = 86,
}

impl TraderExitCode {
    /// The process exit code.
    #[must_use]
    pub const fn code(self) -> u8 {
        self as u8
    }
}

/// Audited supervision phases.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SupervisionPhase {
    /// Boot started.
    Boot,
    /// Kill-switch gate.
    Gate,
    /// No exchange session is scheduled.
    NoSession,
    /// The restart budget is exhausted.
    CrashLoop,
    /// Snapshot restore and recovery.
    Restore,
    /// Market-data connection.
    Connect,
    /// The session is running.
    Running,
    /// Session teardown.
    Teardown,
    /// The session completed.
    Completed,
    /// The session reached its close.
    AtClose,
    /// The session was stopped.
    Stopped,
    /// The session was halted.
    Halted,
    /// The session faulted.
    Faulted,
}

impl SupervisionPhase {
    /// The stable lowercase value written to the status document.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::Boot => "boot",
            Self::Gate => "gate",
            Self::NoSession => "no_session",
            Self::CrashLoop => "crash_loop",
            Self::Restore => "restore",
            Self::Connect => "connect",
            Self::Running => "running",
            Self::Teardown => "teardown",
            Self::Completed => "completed",
            Self::AtClose => "at_close",
            Self::Stopped => "stopped",
            Self::Halted => "halted",
            Self::Faulted => "faulted",
        }
    }
}

/// Terminal supervision outcomes recorded on the restart ledger.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OutcomeKind {
    /// Operator-expected completion.
    Clean,
    /// Kill-switch halt.
    KillSwitch,
    /// Boot policy halt.
    PolicyHalt,
    /// A fault.
    Fault,
}

impl OutcomeKind {
    /// The stable lowercase value.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::Clean => "clean",
            Self::KillSwitch => "kill_switch",
            Self::PolicyHalt => "policy_halt",
            Self::Fault => "fault",
        }
    }
}

/// The boot recovery policy.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BootPolicy {
    /// Resume the snapshot and reconcile.
    ResumeReconcile,
    /// Refuse to resume and halt.
    HaltOnly,
}

impl BootPolicy {
    /// The stable lowercase value.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::ResumeReconcile => "resume_reconcile",
            Self::HaltOnly => "halt_only",
        }
    }

    /// Parse a boot policy value.
    #[must_use]
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "resume_reconcile" => Some(Self::ResumeReconcile),
            "halt_only" => Some(Self::HaltOnly),
            _ => None,
        }
    }
}

/// A supervision configuration or lifecycle failure.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SupervisionError {
    /// The configuration cannot drive a supervised session.
    Configuration(String),
    /// A transient fault the platform may recover by restart.
    Restartable(String),
    /// A terminal fault.
    Fault(String),
}

impl std::fmt::Display for SupervisionError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Configuration(message) | Self::Restartable(message) | Self::Fault(message) => {
                formatter.write_str(message)
            }
        }
    }
}

impl std::error::Error for SupervisionError {}

impl From<LiveError> for SupervisionError {
    fn from(error: LiveError) -> Self {
        Self::Fault(error.to_string())
    }
}

impl From<backtest::runtime::state_store::StateStoreError> for SupervisionError {
    fn from(error: backtest::runtime::state_store::StateStoreError) -> Self {
        Self::Restartable(format!("state store: {error}"))
    }
}

/// Validated supervision knobs with deployment-safe defaults.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SupervisorSettings {
    /// Boot recovery policy.
    pub boot_policy: BootPolicy,
    /// Exact initial paper cash.
    pub initial_cash: Decimal18,
    /// Heartbeat cadence in nanoseconds.
    pub heartbeat_cadence_ns: i64,
    /// Kill-switch poll cadence in nanoseconds.
    pub kill_switch_poll_ns: i64,
    /// Consecutive poll failures tolerated before a restartable fault.
    pub kill_switch_stale: u32,
    /// Periodic snapshot cadence in nanoseconds.
    pub snapshot_cadence_ns: i64,
    /// Continuous reconciliation cadence in nanoseconds.
    pub reconcile_cadence_ns: i64,
    /// Maximum wait for the first market event, in nanoseconds.
    pub market_wait_ns: i64,
    /// Feed connect attempts per connect.
    pub connect_attempts: u32,
    /// Feed reconnect backoff in nanoseconds.
    pub connect_backoff_ns: i64,
    /// Consecutive faulty boots tolerated before a crash-loop refusal.
    pub restart_budget: u32,
}

impl Default for SupervisorSettings {
    fn default() -> Self {
        Self {
            boot_policy: BootPolicy::ResumeReconcile,
            initial_cash: Decimal18::from_unscaled(100_000_000_000_000_000_000_000)
                .unwrap_or(Decimal18::ZERO),
            heartbeat_cadence_ns: 60_000_000_000,
            kill_switch_poll_ns: 10_000_000_000,
            kill_switch_stale: 3,
            snapshot_cadence_ns: 60_000_000_000,
            reconcile_cadence_ns: 900_000_000_000,
            market_wait_ns: 900_000_000_000,
            connect_attempts: 2,
            connect_backoff_ns: 2_000_000_000,
            restart_budget: 3,
        }
    }
}

impl SupervisorSettings {
    /// Validate the supervision knobs.
    ///
    /// # Errors
    ///
    /// Returns [`SupervisionError::Configuration`] for any non-positive cadence
    /// or integer.
    pub fn validate(&self) -> Result<(), SupervisionError> {
        if !self.initial_cash.is_positive() {
            return Err(SupervisionError::Configuration(
                "initial_cash must be positive".to_string(),
            ));
        }
        for (name, value) in [
            ("heartbeat_cadence_ns", self.heartbeat_cadence_ns),
            ("kill_switch_poll_ns", self.kill_switch_poll_ns),
            ("snapshot_cadence_ns", self.snapshot_cadence_ns),
            ("reconcile_cadence_ns", self.reconcile_cadence_ns),
            ("market_wait_ns", self.market_wait_ns),
            ("connect_backoff_ns", self.connect_backoff_ns),
        ] {
            if value <= 0 {
                return Err(SupervisionError::Configuration(format!(
                    "{name} must be a positive duration"
                )));
            }
        }
        for (name, value) in [
            ("kill_switch_stale", self.kill_switch_stale),
            ("connect_attempts", self.connect_attempts),
        ] {
            if value < 1 {
                return Err(SupervisionError::Configuration(format!(
                    "{name} must be a positive integer"
                )));
            }
        }
        Ok(())
    }
}

/// One kill-switch source observation.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct KillSwitchObservation {
    /// Whether the switch is engaged.
    pub engaged: bool,
    /// Optional operator reason.
    pub reason: Option<String>,
}

/// Injected kill-switch source boundary polled by the supervisor.
pub trait KillSwitchPoller {
    /// Return one observation; errors propagate to the supervisor.
    ///
    /// # Errors
    ///
    /// Returns a message when the source cannot be read.
    fn poll(&mut self) -> Result<KillSwitchObservation, String>;
}

/// Injected monotonic wall clock.
pub trait SupervisionClock {
    /// Current wall-clock time in nanoseconds.
    fn now_ns(&self) -> i64;
}

/// A wall clock backed by `std::time::SystemTime`.
#[derive(Clone, Copy, Debug, Default)]
pub struct SystemClock;

impl SupervisionClock for SystemClock {
    fn now_ns(&self) -> i64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|duration| duration.as_nanos() as i64)
            .unwrap_or_default()
    }
}

/// A manually advanced clock for deterministic tests.
#[derive(Clone, Debug, Default)]
pub struct ManualClock {
    now_ns: Rc<Cell<i64>>,
}

impl ManualClock {
    /// Build a manual clock at `now_ns`.
    #[must_use]
    pub fn new(now_ns: i64) -> Self {
        Self {
            now_ns: Rc::new(Cell::new(now_ns)),
        }
    }

    /// Advance the clock by `delta_ns`.
    pub fn advance(&self, delta_ns: i64) {
        self.now_ns.set(self.now_ns.get() + delta_ns);
    }

    /// Set the clock to an absolute instant.
    pub fn set(&self, now_ns: i64) {
        self.now_ns.set(now_ns);
    }
}

impl SupervisionClock for ManualClock {
    fn now_ns(&self) -> i64 {
        self.now_ns.get()
    }
}

/// A market-data feed fault.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum FeedError {
    /// The feed dropped; reconnect with backoff.
    Disconnected(String),
    /// The feed failed terminally.
    Fatal(String),
}

/// The synchronous market-data boundary the supervisor drains.
pub trait MarketFeed {
    /// Return the next event, or `None` at the session close.
    ///
    /// # Errors
    ///
    /// Returns [`FeedError`] when the feed drops or fails.
    fn next_event(&mut self) -> Result<Option<MarketEvent>, FeedError>;
}

/// A feed factory invoked for every connect attempt.
pub type FeedFactory = Box<dyn FnMut() -> Result<Box<dyn MarketFeed>, FeedError>>;

/// A replay/scripted feed over an in-memory event queue.
#[derive(Debug, Default)]
pub struct ScriptedMarketFeed {
    events: std::collections::VecDeque<MarketEvent>,
}

impl ScriptedMarketFeed {
    /// Build a feed over an ordered event script.
    #[must_use]
    pub fn new(events: Vec<MarketEvent>) -> Self {
        Self {
            events: events.into(),
        }
    }
}

impl MarketFeed for ScriptedMarketFeed {
    fn next_event(&mut self) -> Result<Option<MarketEvent>, FeedError> {
        Ok(self.events.pop_front())
    }
}

/// A feed that immediately reaches the session close.
#[derive(Debug, Default)]
pub struct EmptyMarketFeed;

impl MarketFeed for EmptyMarketFeed {
    fn next_event(&mut self) -> Result<Option<MarketEvent>, FeedError> {
        Ok(None)
    }
}

/// Supervised feed that reconnects on disconnect with exponential backoff.
///
/// The reconnect schedule reuses the live transport [`RetryConfig`], so the
/// supervisor, the REST retry wrapper, and the WebSocket boundary all share one
/// backoff policy. The sleeper is injected, so tests never block.
pub struct SupervisedFeed {
    factory: FeedFactory,
    current: Option<Box<dyn MarketFeed>>,
    policy: RetryConfig,
    sleeper: Rc<dyn Fn(i64)>,
    reconnects: u64,
    drops: u64,
}

impl SupervisedFeed {
    /// Build a supervised feed with an explicit backoff policy.
    ///
    /// # Errors
    ///
    /// Returns [`SupervisionError::Configuration`] when the policy is invalid.
    pub fn new(
        factory: FeedFactory,
        connect_attempts: u32,
        connect_backoff_ns: i64,
        sleeper: Rc<dyn Fn(i64)>,
    ) -> Result<Self, SupervisionError> {
        let attempts = connect_attempts.max(1) as usize;
        let backoff_seconds = connect_backoff_ns.max(0) as f64 / 1_000_000_000.0;
        let policy = RetryConfig::new(attempts, backoff_seconds, 30.0, false, 30.0)
            .map_err(|error| SupervisionError::Configuration(error.to_string()))?;
        Ok(Self {
            factory,
            current: None,
            policy,
            sleeper,
            reconnects: 0,
            drops: 0,
        })
    }

    /// The number of reconnects performed.
    #[must_use]
    pub const fn reconnects(&self) -> u64 {
        self.reconnects
    }

    /// The number of dropped connections observed.
    #[must_use]
    pub const fn drops(&self) -> u64 {
        self.drops
    }

    /// Connect once, retrying with backoff up to the configured attempts.
    ///
    /// # Errors
    ///
    /// Returns [`SupervisionError::Restartable`] when every attempt fails.
    pub fn connect(&mut self) -> Result<(), SupervisionError> {
        let mut last = String::from("no attempt was made");
        for attempt in 1..=self.policy.max_attempts {
            match (self.factory)() {
                Ok(feed) => {
                    self.current = Some(feed);
                    return Ok(());
                }
                Err(error) => {
                    let (FeedError::Disconnected(message) | FeedError::Fatal(message)) = error;
                    last = message;
                    if attempt < self.policy.max_attempts {
                        let delay_ns = (self.policy.backoff(attempt) * 1_000_000_000.0) as i64;
                        (self.sleeper)(delay_ns);
                    }
                }
            }
        }
        Err(SupervisionError::Restartable(format!(
            "market-data boundary could not be composed after {} attempts: {last}",
            self.policy.max_attempts
        )))
    }

    /// Return the next event, reconnecting transparently on a drop.
    ///
    /// # Errors
    ///
    /// Returns [`SupervisionError::Restartable`] when reconnects are exhausted
    /// and [`SupervisionError::Fault`] on a terminal feed fault.
    pub fn next_event(&mut self) -> Result<Option<MarketEvent>, SupervisionError> {
        loop {
            if self.current.is_none() {
                self.connect()?;
            }
            let feed = self
                .current
                .as_mut()
                .ok_or_else(|| SupervisionError::Fault("feed was not connected".to_string()))?;
            match feed.next_event() {
                Ok(option) => return Ok(option),
                Err(FeedError::Fatal(message)) => return Err(SupervisionError::Fault(message)),
                Err(FeedError::Disconnected(_message)) => {
                    self.drops += 1;
                    self.current = None;
                    self.reconnects += 1;
                    self.connect()?;
                }
            }
        }
    }
}

/// The resolved trader configuration the supervisor consumes.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TraderConfig {
    /// Configured execution job identifier, when pinned.
    pub job_id: Option<String>,
    /// Deployment name metadata; never the execution identity.
    pub deployment_name: String,
    /// `paper` or `shadow` mode.
    pub mode: String,
    /// Run seed.
    pub seed: u64,
    /// Strategy instance identifiers.
    pub strategy_instance_ids: Vec<String>,
    /// Risk rejection policy.
    pub on_reject: String,
    /// Configured risk rule identifiers.
    pub rule_ids: Vec<String>,
}

impl TraderConfig {
    /// Load and validate one trader configuration document.
    ///
    /// # Errors
    ///
    /// Returns [`SupervisionError::Configuration`] when the document is not a
    /// valid trader shell.
    pub fn load(path: &std::path::Path) -> Result<Self, SupervisionError> {
        let loaded = config::load_configuration(path).map_err(|error| {
            SupervisionError::Configuration(format!("configuration could not be loaded: {error}"))
        })?;
        let value = match loaded {
            config::HarnessConfig::Trader(value) => value,
            other => {
                return Err(SupervisionError::Configuration(format!(
                    "configuration shell must be 'trader', not '{}'",
                    other.shell()
                )))
            }
        };
        Self::from_value(&value)
    }

    /// Resolve the typed fields from the canonical configuration tree.
    ///
    /// # Errors
    ///
    /// Returns [`SupervisionError::Configuration`] when a required field is
    /// missing or misshaped.
    pub fn from_value(value: &Value) -> Result<Self, SupervisionError> {
        let mode = value
            .get("mode")
            .and_then(Value::as_str)
            .ok_or_else(|| SupervisionError::Configuration("mode is required".to_string()))?;
        if mode != "paper" && mode != "shadow" {
            return Err(SupervisionError::Configuration(format!(
                "supervised sessions support paper and shadow modes, not {mode:?}"
            )));
        }
        let seed = value.get("seed").and_then(Value::as_u64).unwrap_or(0);
        let job_id = match value.get("job_id") {
            None | Some(Value::Null) => None,
            Some(Value::String(text)) => Some(job_identifier(text, "job_id")?),
            Some(_) => {
                return Err(SupervisionError::Configuration(
                    "job_id must be a string".to_string(),
                ))
            }
        };
        let name = value
            .get("deployment")
            .and_then(|deployment| deployment.get("name"))
            .and_then(Value::as_str)
            .unwrap_or("trader");
        let deployment_name = job_identifier(name, "deployment.name")?;
        let strategy_instance_ids = value
            .get("strategy_instances")
            .and_then(Value::as_array)
            .map(|instances| {
                instances
                    .iter()
                    .filter_map(|instance| {
                        instance
                            .get("instance_id")
                            .and_then(Value::as_str)
                            .map(str::to_string)
                    })
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();
        if strategy_instance_ids.is_empty() {
            return Err(SupervisionError::Configuration(
                "strategy_instances must not be empty".to_string(),
            ));
        }
        let on_reject = value
            .get("risk_pipeline")
            .and_then(|pipeline| pipeline.get("on_reject"))
            .and_then(Value::as_str)
            .unwrap_or("reject_order")
            .to_string();
        let rule_ids = value
            .get("risk_pipeline")
            .and_then(|pipeline| pipeline.get("rules"))
            .and_then(Value::as_array)
            .map(|rules| {
                rules
                    .iter()
                    .filter_map(|rule| {
                        rule.get("rule_id")
                            .and_then(Value::as_str)
                            .map(str::to_string)
                    })
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();
        Ok(Self {
            job_id,
            deployment_name,
            mode: mode.to_string(),
            seed,
            strategy_instance_ids,
            on_reject,
            rule_ids,
        })
    }

    /// The paper session policy for this mode.
    #[must_use]
    pub fn session_policy(&self) -> PaperSessionPolicy {
        PaperSessionPolicy::Session
    }
}

/// Shared, cheaply cloneable snapshot object store.
///
/// Wraps a store behind `Rc<RefCell<_>>` so the supervision journal and the
/// runtime state store observe one another's writes within a single process.
#[derive(Clone)]
pub struct SharedSnapshotStore {
    inner: Rc<RefCell<Box<dyn SnapshotObjectStore>>>,
}

impl SharedSnapshotStore {
    /// Wrap a store.
    #[must_use]
    pub fn new(store: Box<dyn SnapshotObjectStore>) -> Self {
        Self {
            inner: Rc::new(RefCell::new(store)),
        }
    }

    /// Build an in-memory shared store.
    #[must_use]
    pub fn in_memory() -> Self {
        Self::new(Box::new(InMemorySnapshotStore::new()))
    }

    /// Write one UTF-8 object.
    pub fn write_text(&self, path: &str, text: &str) {
        self.inner.borrow_mut().replace(path, text.as_bytes());
    }

    /// Read one UTF-8 object.
    #[must_use]
    pub fn read_text(&self, path: &str) -> Option<String> {
        self.inner
            .borrow()
            .read(path)
            .and_then(|bytes| String::from_utf8(bytes).ok())
    }
}

impl SnapshotObjectStore for SharedSnapshotStore {
    fn replace(&mut self, relative_path: &str, payload: &[u8]) {
        self.inner.borrow_mut().replace(relative_path, payload);
    }

    fn read(&self, relative_path: &str) -> Option<Vec<u8>> {
        self.inner.borrow().read(relative_path)
    }
}

/// A local filesystem snapshot store for the trader binary.
pub struct LocalFileSnapshotStore {
    root: PathBuf,
}

impl LocalFileSnapshotStore {
    /// Build a store rooted at `root`.
    #[must_use]
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self { root: root.into() }
    }

    fn resolve(&self, relative_path: &str) -> PathBuf {
        self.root.join(relative_path)
    }
}

impl SnapshotObjectStore for LocalFileSnapshotStore {
    fn replace(&mut self, relative_path: &str, payload: &[u8]) {
        let path = self.resolve(relative_path);
        if let Some(parent) = path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        let _ = std::fs::write(path, payload);
    }

    fn read(&self, relative_path: &str) -> Option<Vec<u8>> {
        std::fs::read(self.resolve(relative_path)).ok()
    }
}

/// Durable supervision phase, heartbeat, and restart-ledger records.
pub struct SupervisionJournal {
    store: SharedSnapshotStore,
    job_id: String,
    clock: Rc<dyn SupervisionClock>,
}

impl SupervisionJournal {
    /// Bind a journal to one execution job.
    #[must_use]
    pub fn new(
        store: SharedSnapshotStore,
        job_id: impl Into<String>,
        clock: Rc<dyn SupervisionClock>,
    ) -> Self {
        Self {
            store,
            job_id: job_id.into(),
            clock,
        }
    }

    /// The execution job identifier this journal records.
    #[must_use]
    pub fn job_id(&self) -> &str {
        &self.job_id
    }

    fn status_key(&self) -> String {
        format!("runs/{}/supervision.json", self.job_id)
    }

    fn ledger_key(&self) -> String {
        format!("runs/{}/restarts.json", self.job_id)
    }

    /// The consecutive-fault count on the restart ledger.
    #[must_use]
    pub fn consecutive_faults(&self) -> u32 {
        self.read_ledger()
            .get("consecutive_faults")
            .and_then(Value::as_u64)
            .unwrap_or(0) as u32
    }

    /// The full restart ledger document.
    #[must_use]
    pub fn read_ledger(&self) -> Value {
        self.store
            .read_text(&self.ledger_key())
            .and_then(|text| serde_json::from_str(&text).ok())
            .unwrap_or_else(|| {
                json!({
                    "format_version": 1,
                    "job_id": self.job_id,
                    "consecutive_faults": 0,
                    "history": [],
                })
            })
    }

    /// Replace the supervision status document.
    pub fn record_status(
        &self,
        phase: SupervisionPhase,
        detail: &str,
        market_event_count: u64,
        snapshot_id: Option<&str>,
        heartbeat: bool,
    ) {
        let now = self.clock.now_ns();
        let document = json!({
            "format_version": 1,
            "job_id": self.job_id,
            "phase": phase.value(),
            "detail": detail,
            "updated_at_ns": now,
            "heartbeat": heartbeat,
            "market_event_count": market_event_count,
            "snapshot_id": snapshot_id,
            "consecutive_faults": self.consecutive_faults(),
        });
        if let Ok(text) = serde_json::to_string(&document) {
            self.store.write_text(&self.status_key(), &text);
        }
    }

    /// Fold one terminal boot outcome into the restart ledger.
    pub fn record_boot_outcome(&self, kind: OutcomeKind, detail: &str) {
        let mut ledger = self.read_ledger();
        let faults = ledger
            .get("consecutive_faults")
            .and_then(Value::as_u64)
            .unwrap_or(0);
        let faults = match kind {
            OutcomeKind::Fault => faults + 1,
            OutcomeKind::Clean | OutcomeKind::KillSwitch => 0,
            OutcomeKind::PolicyHalt => faults,
        };
        let entry = json!({
            "at_ns": self.clock.now_ns(),
            "outcome": kind.value(),
            "detail": detail,
        });
        let mut history = ledger
            .get("history")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        history.push(entry);
        if history.len() > LEDGER_HISTORY_LIMIT {
            let drop = history.len() - LEDGER_HISTORY_LIMIT;
            history.drain(0..drop);
        }
        ledger = json!({
            "format_version": 1,
            "job_id": self.job_id,
            "consecutive_faults": faults,
            "history": history,
        });
        if let Ok(text) = serde_json::to_string(&ledger) {
            self.store.write_text(&self.ledger_key(), &text);
        }
    }

    /// The latest supervision status document, or `None`.
    #[must_use]
    pub fn read_status(&self) -> Option<Value> {
        self.store
            .read_text(&self.status_key())
            .and_then(|text| serde_json::from_str(&text).ok())
    }
}

/// The one exchange session a supervisor drives.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SessionWindow {
    /// UTC session open in epoch nanoseconds.
    pub open_ns: i64,
    /// UTC session close in epoch nanoseconds.
    pub close_ns: i64,
}

/// The terminal result of one supervised boot.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SupervisorOutcome {
    /// Execution job identifier.
    pub job_id: String,
    /// Process exit code.
    pub exit_code: TraderExitCode,
    /// Terminal phase.
    pub phase: SupervisionPhase,
    /// Human-readable detail.
    pub detail: Option<String>,
    /// Market events consumed.
    pub market_event_count: u64,
    /// Latest persisted snapshot identity, if any.
    pub snapshot_id: Option<String>,
}

impl SupervisorOutcome {
    /// Whether the boot ended as an operator-expected outcome.
    #[must_use]
    pub fn clean(&self) -> bool {
        matches!(
            self.exit_code,
            TraderExitCode::Success | TraderExitCode::KillSwitchHalted
        )
    }
}

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
    ) -> Result<Option<backtest::runtime::state_store::RuntimeSnapshot>, SupervisionError> {
        let snapshot = match self.state_store.load() {
            Ok(snapshot) => snapshot,
            Err(backtest::runtime::state_store::StateStoreError::Missing(_)) => return Ok(None),
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
        let position_deltas = reconcile_positions(&expected, &actual, tolerances);
        let expected_cash = self.settings.initial_cash.to_string();
        let actual_cash = shell
            .books()
            .first()
            .map_or_else(|| expected_cash.clone(), |book| book.cash.clone());
        let cash_delta = reconcile_cash(&expected_cash, &actual_cash, tolerances);
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

/// The environment variable pinning the trader execution job identifier.
pub const TRADER_JOB_ID_ENV: &str = "HARNESS_TRADER_JOB_ID";

fn default_state_base() -> PathBuf {
    PathBuf::from(".local").join("trader-state")
}

fn deployment_state_root(base: &std::path::Path, deployment_name: &str) -> PathBuf {
    base.join(deployment_name)
}

fn job_identifier(value: &str, field: &str) -> Result<String, SupervisionError> {
    let text = value.trim();
    let valid = text
        .chars()
        .next()
        .is_some_and(|first| first.is_ascii_alphanumeric())
        && text
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '.' | '_' | '-'));
    if !valid {
        return Err(SupervisionError::Configuration(format!(
            "{field} contains invalid characters"
        )));
    }
    Ok(text.to_string())
}

fn allocate_job_id() -> String {
    backtest::backtest::manifest::generate_job_id()
}

/// Resolve the one execution job identifier for a supervised process.
///
/// # Errors
///
/// Returns [`SupervisionError::Configuration`] when the configured identifier
/// conflicts with the persisted execution identity.
pub fn resolve_job_id(
    configured: Option<&str>,
    persisted: Option<&str>,
) -> Result<String, SupervisionError> {
    match (configured, persisted) {
        (Some(configured), Some(persisted)) if configured != persisted => {
            Err(SupervisionError::Configuration(format!(
                "configured job {configured:?} does not match persisted job {persisted:?}"
            )))
        }
        (Some(configured), _) => Ok(configured.to_string()),
        (None, Some(persisted)) => Ok(persisted.to_string()),
        (None, None) => Ok(allocate_job_id()),
    }
}

fn requested_job_id(configured: Option<&str>) -> Result<Option<String>, SupervisionError> {
    let pinned = std::env::var(TRADER_JOB_ID_ENV)
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty());
    match (configured, pinned.as_deref()) {
        (Some(configured), Some(pinned)) if configured != pinned => {
            Err(SupervisionError::Configuration(format!(
                "configured job {configured:?} does not match {TRADER_JOB_ID_ENV} {pinned:?}"
            )))
        }
        (Some(configured), _) => Ok(Some(job_identifier(configured, "job_id")?)),
        (None, Some(pinned)) => Ok(Some(job_identifier(pinned, TRADER_JOB_ID_ENV)?)),
        (None, None) => Ok(None),
    }
}

fn read_persisted_job_id(root: &std::path::Path) -> Result<Option<String>, SupervisionError> {
    let path = root.join("current.json");
    let text = match std::fs::read_to_string(&path) {
        Ok(text) => text,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => {
            return Err(SupervisionError::Configuration(format!(
                "persisted job identity at {} could not be read: {error}",
                path.display()
            )))
        }
    };
    let document: Value = serde_json::from_str(&text).map_err(|error| {
        SupervisionError::Configuration(format!(
            "persisted job identity at {} is not valid JSON: {error}",
            path.display()
        ))
    })?;
    let job_id = document
        .get("job_id")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            SupervisionError::Configuration(format!(
                "persisted job identity at {} does not carry job_id",
                path.display()
            ))
        })?;
    Ok(Some(job_identifier(job_id, "persisted job_id")?))
}

fn persist_job_id(root: &std::path::Path, job_id: &str) -> Result<(), SupervisionError> {
    std::fs::create_dir_all(root).map_err(|error| {
        SupervisionError::Configuration(format!(
            "job state root {} could not be created: {error}",
            root.display()
        ))
    })?;
    let document = json!({"format_version": 1, "job_id": job_id});
    let text = serde_json::to_string(&document).map_err(|error| {
        SupervisionError::Configuration(format!("job identity could not be encoded: {error}"))
    })?;
    let path = root.join("current.json");
    let temporary = root.join("current.json.tmp");
    std::fs::write(&temporary, text).map_err(|error| {
        SupervisionError::Configuration(format!(
            "job identity at {} could not be persisted: {error}",
            temporary.display()
        ))
    })?;
    std::fs::rename(&temporary, &path).map_err(|error| {
        SupervisionError::Configuration(format!(
            "job identity at {} could not be persisted: {error}",
            path.display()
        ))
    })
}

fn resolve_process_job_id(
    deployment_name: &str,
    configured: Option<&str>,
) -> Result<String, SupervisionError> {
    let requested = requested_job_id(configured)?;
    let root = deployment_state_root(&default_state_base(), deployment_name);
    let persisted = read_persisted_job_id(&root)?;
    let job_id = resolve_job_id(requested.as_deref(), persisted.as_deref())?;
    if persisted.as_deref() != Some(job_id.as_str()) {
        persist_job_id(&root, &job_id)?;
    }
    Ok(job_id)
}

/// Compose and run one supervised trader process from a configuration path.
///
/// This is the deployment composition used by both process entrypoints: it
/// resolves the execution job identifier, binds the durable local state store
/// and supervision journal, and runs one supervised paper or shadow session.
/// The market-data boundary is the deterministic in-process feed; the live
/// adapter is supplied by the runtime environment.
#[must_use]
pub fn run_trader_process(config_path: &std::path::Path) -> TraderExitCode {
    let mut configuration = match TraderConfig::load(config_path) {
        Ok(configuration) => configuration,
        Err(error) => {
            eprintln!("{error}");
            return TraderExitCode::Usage;
        }
    };
    let job_id = match resolve_process_job_id(
        &configuration.deployment_name,
        configuration.job_id.as_deref(),
    ) {
        Ok(job_id) => job_id,
        Err(error) => {
            eprintln!("{error}");
            return TraderExitCode::Usage;
        }
    };
    configuration.job_id = Some(job_id.clone());
    let clock = Rc::new(SystemClock);
    let shared = SharedSnapshotStore::new(Box::new(LocalFileSnapshotStore::new(
        deployment_state_root(&default_state_base(), &configuration.deployment_name),
    )));
    let mut state_store = RuntimeStateStore::new(&job_id);
    state_store.add_store(Box::new(shared.clone()));
    let journal = SupervisionJournal::new(shared, &job_id, clock.clone());
    let settings = SupervisorSettings::default();
    let now_ns = clock.now_ns();
    let session = Some(SessionWindow {
        open_ns: now_ns,
        close_ns: now_ns + DEFAULT_SESSION_NS,
    });
    let feed_factory: FeedFactory =
        Box::new(|| Ok(Box::new(EmptyMarketFeed) as Box<dyn MarketFeed>));
    let sleeper: Rc<dyn Fn(i64)> = Rc::new(|nanos: i64| {
        if nanos > 0 {
            std::thread::sleep(std::time::Duration::from_nanos(nanos as u64));
        }
    });
    let supervised_feed = match SupervisedFeed::new(
        feed_factory,
        settings.connect_attempts,
        settings.connect_backoff_ns,
        sleeper,
    ) {
        Ok(feed) => feed,
        Err(error) => {
            eprintln!("{error}");
            return TraderExitCode::Failed;
        }
    };
    let kill_switch = match KillSwitch::new(None) {
        Ok(switch) => switch,
        Err(error) => {
            eprintln!("{error}");
            return TraderExitCode::Failed;
        }
    };
    let mut supervisor = match TraderSupervisor::new(
        configuration,
        settings,
        session,
        state_store,
        journal,
        clock,
        supervised_feed,
        kill_switch,
        None,
        Box::new(|line| println!("{line}")),
    ) {
        Ok(supervisor) => supervisor,
        Err(error) => {
            eprintln!("{error}");
            return TraderExitCode::Usage;
        }
    };
    let outcome = supervisor.run();
    if let Some(detail) = &outcome.detail {
        eprintln!("{detail}");
    }
    outcome.exit_code
}

/// Read the latest supervision document for a configured execution job.
#[must_use]
pub fn read_supervision_status(config_path: &std::path::Path) -> Option<String> {
    let configuration = TraderConfig::load(config_path).ok()?;
    let root = deployment_state_root(&default_state_base(), &configuration.deployment_name);
    let job_id = match requested_job_id(configuration.job_id.as_deref()).ok()? {
        Some(job_id) => job_id,
        None => read_persisted_job_id(&root).ok()??,
    };
    let shared = SharedSnapshotStore::new(Box::new(LocalFileSnapshotStore::new(root)));
    shared.read_text(&format!("runs/{job_id}/supervision.json"))
}
