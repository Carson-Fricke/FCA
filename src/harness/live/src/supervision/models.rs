//! Owns trader supervision models; lifecycle coordination remains in the parent.

use harness_trading::error::LiveError;

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

impl From<crate::runtime::state_store::StateStoreError> for SupervisionError {
    fn from(error: crate::runtime::state_store::StateStoreError) -> Self {
        Self::Restartable(format!("state store: {error}"))
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
