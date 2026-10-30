//! Owns MCP consumer ports and context construction; adapters supply implementations.
use std::collections::BTreeMap;
use std::fmt;
use std::sync::Arc;

use serde_json::json;
use serde_json::Map;
use serde_json::Value;

use crate::auth::BearerTokenSource;
use crate::auth::BEARER_TOKEN_VARIABLE;

/// The `run_backtest` execution boundary.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Boundary {
    /// Invoke the configured local process runner.
    Local,
    /// Post to the remote harness API.
    Remote,
}

impl Boundary {
    /// Parse the documented wire spelling.
    ///
    /// # Errors
    ///
    /// Returns [`ContextError`] for any other value.
    pub fn parse(value: &str) -> Result<Self, ContextError> {
        match value {
            "local" => Ok(Self::Local),
            "remote" => Ok(Self::Remote),
            other => Err(ContextError(format!(
                "mode must be 'local' or 'remote', got '{other}'"
            ))),
        }
    }

    /// The wire spelling of this boundary.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Local => "local",
            Self::Remote => "remote",
        }
    }
}

/// A tool-level failure reported as an `isError` MCP result.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ToolExecutionError {
    message: String,
    payload: Value,
}

impl ToolExecutionError {
    /// Add observed boundary certainty without dropping structured server details.
    #[must_use]
    pub fn with_certainty(mut self, certainty: &str) -> Self {
        let mut payload = match std::mem::take(&mut self.payload) {
            Value::Object(payload) => payload,
            value => Map::from_iter([("serverError".to_string(), value)]),
        };
        payload.insert("executionCertainty".into(), json!(certainty));
        self.payload = Value::Object(payload);
        self
    }

    /// Construct an error with a structured category and explanation.
    #[must_use]
    pub fn new(message: impl Into<String>) -> Self {
        Self::with_payload(message, json!({}))
    }

    /// Construct an error carrying a structured outcome payload.
    #[must_use]
    pub fn with_payload(message: impl Into<String>, payload: Value) -> Self {
        let message = message.into();
        let mut payload = match payload {
            Value::Object(payload) => payload,
            value => Map::from_iter([("serverError".to_string(), value)]),
        };
        payload.entry("kind").or_insert(json!("tool"));
        payload.entry("message").or_insert(json!(message));
        Self {
            message,
            payload: Value::Object(payload),
        }
    }

    /// The human-readable message.
    #[must_use]
    pub fn message(&self) -> &str {
        &self.message
    }

    /// The structured payload; every constructed error has one.
    #[must_use]
    pub fn payload(&self) -> Option<&Value> {
        Some(&self.payload)
    }
}

impl fmt::Display for ToolExecutionError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for ToolExecutionError {}

/// Fail-closed declaration of the tools one server publishes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ToolCapabilities {
    /// Publish `run_backtest`.
    pub backtest: bool,
    /// Publish `get_job_status`.
    pub job_status: bool,
    /// Publish `get_job_result` and `list_artifacts`.
    pub artifacts: bool,
    /// Publish `run_study`.
    pub study: bool,
    /// Publish `run_screener` and the template.
    pub screener: bool,
    /// Publish `launch_trader` (opt-in).
    pub trader: bool,
}

impl Default for ToolCapabilities {
    fn default() -> Self {
        Self {
            backtest: true,
            job_status: true,
            artifacts: true,
            study: true,
            screener: true,
            trader: false,
        }
    }
}

impl ToolCapabilities {
    /// Return the declaration keyed by capability name.
    #[must_use]
    pub fn as_mapping(&self) -> Value {
        json!({
            "backtest": self.backtest,
            "job_status": self.job_status,
            "artifacts": self.artifacts,
            "study": self.study,
            "screener": self.screener,
            "trader": self.trader,
        })
    }
}

/// One local harness process invocation's observed outcome.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProcessOutcome {
    /// Process exit code.
    pub returncode: i32,
    /// Captured standard output.
    pub stdout: String,
    /// Captured standard error.
    pub stderr: String,
}

/// Injected local process boundary; no subprocess is created here.
pub trait ProcessRunner: Send + Sync {
    /// Run one argv and capture its observed outcome.
    ///
    /// # Errors
    ///
    /// Returns a reason string when the process cannot be invoked.
    fn run(&self, argv: &[String]) -> Result<ProcessOutcome, String>;
}

/// One decoded remote harness API response.
#[derive(Debug, Clone, PartialEq)]
pub struct RemoteResponse {
    /// HTTP status code.
    pub status_code: u16,
    /// Response headers.
    pub headers: BTreeMap<String, String>,
    /// Decoded JSON body.
    pub body: Value,
}

/// HTTP request surface implemented by a real client or a test fake.
pub trait RemoteTransport: Send + Sync {
    /// Perform one authenticated GET.
    ///
    /// # Errors
    ///
    /// Returns a reason string when the request cannot be completed.
    fn get(&self, url: &str, headers: &BTreeMap<String, String>) -> Result<RemoteResponse, String>;
    /// Perform one authenticated POST with a JSON body.
    ///
    /// # Errors
    ///
    /// Returns a reason string when the request cannot be completed.
    fn post(
        &self,
        url: &str,
        headers: &BTreeMap<String, String>,
        payload: &Value,
    ) -> Result<RemoteResponse, String>;
}

/// Injected local backtest surface for one configuration document path.
pub trait BacktestSubmitter: Send + Sync {
    /// Run one backtest and return its run summary.
    ///
    /// # Errors
    ///
    /// Returns a reason string when the backtest cannot be completed.
    fn submit(
        &self,
        config_path: &str,
        workspace: Option<&str>,
        job_id: Option<&str>,
    ) -> Result<Value, String>;
}

/// Injected local study surface for one study configuration document path.
pub trait StudySubmitter: Send + Sync {
    /// Run one study and return its study summary.
    ///
    /// # Errors
    ///
    /// Returns a reason string when the study cannot be completed.
    fn submit(
        &self,
        config_path: &str,
        workspace: Option<&str>,
        job_id: Option<&str>,
    ) -> Result<Value, String>;
}

/// Injected local screening surface for one screen configuration document path.
pub trait ScreeningSubmitter: Send + Sync {
    /// Launch one screening run and return its run summary.
    ///
    /// # Errors
    ///
    /// Returns a reason string when the screening run cannot be launched.
    fn submit(
        &self,
        config_path: &str,
        workspace: Option<&str>,
        job_id: Option<&str>,
    ) -> Result<Value, String>;
}

/// Read-only compiler SDK reference capability; cannot compile or execute authored programs.
pub trait SdkReference: Send + Sync {
    /// Serving compiler's SDK identity and source guidance, never a client-local SDK.
    fn sdk_reference(&self, arguments: &Map<String, Value>) -> Result<Value, ToolExecutionError>;
}

/// Injected local study validation surface for one authored model document.
///
/// Validation compiles, loads, and constructs the authored program exactly as
/// a run would, but never replays market data: the authoring seam fails while
/// the contract is still repairable.
pub trait StudyValidator: Send + Sync {
    /// Compile, load, and construct a strategy; no structural-only fallback.
    fn validate_strategy_result(&self, _model: &Value) -> Result<Value, ToolExecutionError> {
        Err(ToolExecutionError::new(
            "strategy validation is not implemented by the composed validator",
        ))
    }

    /// Opaque provenance supplied by the validator, never a client cache key.
    fn validation_identity(&self) -> Option<String> {
        None
    }

    /// Validate with structured diagnostics and validator-owned reuse metadata.
    fn validate_result(&self, model: &Value) -> Result<Value, ToolExecutionError> {
        self.validate(model).map_err(ToolExecutionError::new)
    }

    /// Validate one authored model document by compiling its program.
    ///
    /// # Errors
    ///
    /// Returns actionable diagnostics when the program does not compile or
    /// omits the SDK handshake exports.
    fn validate(&self, model: &Value) -> Result<Value, String>;
}

/// Loads and validates one configuration document as a JSON payload.
pub trait ConfigurationLoader: Send + Sync {
    /// Load one configuration path into the JSON launch body.
    ///
    /// # Errors
    ///
    /// Returns a reason string when the document cannot be loaded.
    fn load_json(&self, path: &str) -> Result<Value, String>;
}

/// Invalid tool-context composition.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ContextError(pub String);

impl fmt::Display for ContextError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl std::error::Error for ContextError {}

/// Injected boundaries and settings behind the tool handlers.
pub struct HarnessToolsContext {
    /// Selected `run_backtest` boundary.
    pub mode: Boundary,
    /// Bearer credential source for remote calls.
    pub credentials: Arc<dyn BearerTokenSource>,
    /// Published capability declaration.
    pub capabilities: ToolCapabilities,
    /// Local program prefix.
    pub program: Vec<String>,
    /// Remote harness API base URL.
    pub api_base_url: Option<String>,
    /// Remote transport boundary.
    pub remote_transport: Option<Arc<dyn RemoteTransport>>,
    /// Local process runner.
    pub runner: Option<Arc<dyn ProcessRunner>>,
    /// Local backtest submitter.
    pub backtest_submitter: Option<Arc<dyn BacktestSubmitter>>,
    /// Local study submitter.
    pub study_submitter: Option<Arc<dyn StudySubmitter>>,
    /// Local screening submitter.
    pub screening_submitter: Option<Arc<dyn ScreeningSubmitter>>,
    /// Local study validator (compiles authored programs without replaying).
    pub study_validator: Option<Arc<dyn StudyValidator>>,
    /// Read-only SDK source and identity provider.
    pub sdk_reference: Option<Arc<dyn SdkReference>>,
    /// Durable local job store, queried in `local` mode.
    pub job_store: Option<Arc<dyn harness_execution::jobs::JobReader>>,
    /// Configuration loader for remote launches.
    pub configuration_loader: Arc<dyn ConfigurationLoader>,
}

impl HarnessToolsContext {
    /// Compose a context with defaults for the non-essential boundaries.
    #[must_use]
    pub fn new(mode: Boundary, credentials: Arc<dyn BearerTokenSource>) -> Self {
        Self {
            mode,
            credentials,
            capabilities: ToolCapabilities::default(),
            program: vec!["harness".to_string()],
            api_base_url: None,
            remote_transport: None,
            runner: None,
            backtest_submitter: None,
            study_submitter: None,
            screening_submitter: None,
            study_validator: None,
            sdk_reference: None,
            job_store: None,
            configuration_loader: Arc::new(super::arguments::SharedFileConfigurationLoader),
        }
    }

    /// Validate the composed context exactly like `__post_init__`.
    ///
    /// # Errors
    ///
    /// Returns [`ContextError`] for a blank program, a non-HTTPS base URL, or
    /// a boundary missing its required transport.
    pub fn validate(mut self) -> Result<Self, ContextError> {
        if self.program.is_empty() || self.program.iter().any(|part| part.trim().is_empty()) {
            return Err(ContextError(
                "program must be a non-empty sequence of non-empty strings".to_string(),
            ));
        }
        if let Some(base_url) = self.api_base_url.take() {
            if base_url.trim().is_empty() {
                return Err(ContextError(
                    "api_base_url must be a non-empty string".to_string(),
                ));
            }
            if !base_url.trim().to_lowercase().starts_with("https://") {
                return Err(ContextError(
                    "remote harness api_base_url must use HTTPS".to_string(),
                ));
            }
            self.api_base_url = Some(base_url.trim().trim_end_matches('/').to_string());
        }
        match self.mode {
            Boundary::Remote => {
                if self.api_base_url.is_none() || self.remote_transport.is_none() {
                    return Err(ContextError(
                        "remote execution requires an https api_base_url and a RemoteTransport"
                            .to_string(),
                    ));
                }
            }
            // Discovery and templates need no execution authority. Each operation
            // requires its own port when called.
            Boundary::Local => {}
        }
        Ok(self)
    }

    /// Serialize deployment settings without any credential value.
    #[must_use]
    pub fn as_settings(&self) -> Value {
        json!({
            "mode": self.mode.as_str(),
            "program": self.program,
            "api_base_url": self.api_base_url,
            "bearer_env_var": BEARER_TOKEN_VARIABLE,
            "bearer_configured": self.credentials.bearer_token().is_some(),
            "capabilities": self.capabilities.as_mapping(),
        })
    }
}
