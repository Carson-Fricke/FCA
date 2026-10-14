//! Tool catalog and execution boundaries for the harness MCP server.
//!
//! The registry publishes `run_backtest`, `get_job_status`, `get_job_result`,
//! `list_artifacts`, `read_artifact`, `run_study`, `validate_study`,
//! `run_screener`, the configuration templates, and the capability-gated
//! `launch_trader`. Every boundary is
//! injected; the server composes no subprocess or HTTP client itself, and
//! serialized settings never contain a token value.

use std::collections::BTreeMap;
use std::fmt;
use std::sync::Arc;

use percent_encoding::{utf8_percent_encode, AsciiSet, NON_ALPHANUMERIC};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use crate::auth::{BearerTokenSource, BEARER_TOKEN_VARIABLE};
use crate::store::{artifact_digest, artifact_prefix, JobStore};

/// Tool name for the backtest runner.
pub const TOOL_RUN_BACKTEST: &str = "run_backtest";
/// Tool name for the embedded backtest configuration template.
pub const TOOL_BACKTEST_CONFIG_TEMPLATE: &str = "backtest_config_template";
/// Tool name for job status queries.
pub const TOOL_JOB_STATUS: &str = "get_job_status";
/// Tool name for stored job result queries.
pub const TOOL_GET_JOB_RESULT: &str = "get_job_result";
/// Tool name for artifact listing.
pub const TOOL_LIST_ARTIFACTS: &str = "list_artifacts";
/// Tool name for artifact content reads.
pub const TOOL_READ_ARTIFACT: &str = "read_artifact";
/// Tool name for the capability-gated trader launch.
pub const TOOL_LAUNCH_TRADER: &str = "launch_trader";
/// Tool name for the study runner.
pub const TOOL_RUN_STUDY: &str = "run_study";
/// Tool name for study-source validation without a replay.
pub const TOOL_VALIDATE_STUDY: &str = "validate_study";
/// Tool name for strategy-source validation without a replay.
pub const TOOL_VALIDATE_STRATEGY: &str = "validate_strategy";
/// Read the serving compiler's SDK identity and bounded source guidance.
pub const TOOL_READ_SDK_REFERENCE: &str = "read_sdk_reference";
/// Tool name for the screening runner.
pub const TOOL_RUN_SCREENER: &str = "run_screener";
/// Tool name for the embedded screening configuration template.
pub const TOOL_SCREENER_CONFIG_TEMPLATE: &str = "screener_config_template";

const LAUNCH_ROUTE: &str = "/v1/backtests/jobs";
const JOB_ROUTE: &str = "/v1/jobs";
const STUDY_LAUNCH_ROUTE: &str = "/v1/studies/jobs";
/// The screening launch route Thesis and remote MCP clients call.
pub const SCREENER_LAUNCH_ROUTE: &str = "/v1/screeners/jobs";
const IDENTIFIER_MAX: usize = 128;

/// Canonical starter configuration embedded at build time so an agent can
/// author a backtest when no configuration file exists.
const BACKTEST_CONFIG_TEMPLATE: &str = include_str!(concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../../config/harness/backtest.yaml"
));

/// Canonical starter screening configuration embedded at build time.
const SCREENER_CONFIG_TEMPLATE: &str = include_str!(concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../../config/harness/screener.yaml"
));

/// Characters left unescaped when encoding a path segment (RFC 3986 unreserved).
const PATH_SEGMENT: &AsciiSet = &NON_ALPHANUMERIC
    .remove(b'-')
    .remove(b'_')
    .remove(b'.')
    .remove(b'~');

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
    payload: Option<Value>,
}

impl ToolExecutionError {
    /// Add observed boundary certainty without dropping structured server details.
    #[must_use]
    pub fn with_certainty(mut self, certainty: &str) -> Self {
        let mut payload = match self.payload.take() {
            Some(Value::Object(payload)) => payload,
            Some(value) => Map::from_iter([("serverError".to_string(), value)]),
            None => Map::new(),
        };
        payload.insert("executionCertainty".into(), json!(certainty));
        self.payload = Some(Value::Object(payload));
        self
    }

    /// Construct an error with no structured payload.
    #[must_use]
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            payload: None,
        }
    }

    /// Construct an error carrying a structured outcome payload.
    #[must_use]
    pub fn with_payload(message: impl Into<String>, payload: Value) -> Self {
        Self {
            message: message.into(),
            payload: Some(payload),
        }
    }

    /// The human-readable message.
    #[must_use]
    pub fn message(&self) -> &str {
        &self.message
    }

    /// The optional structured payload.
    #[must_use]
    pub fn payload(&self) -> Option<&Value> {
        self.payload.as_ref()
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

/// Injected local study validation surface for one authored model document.
///
/// Validation compiles, loads, and constructs the authored program exactly as
/// a run would, but never replays market data: the authoring seam fails while
/// the contract is still repairable.
pub trait StudyValidator: Send + Sync {
    /// Serving compiler's SDK identity and source guidance, never a client-local SDK.
    fn sdk_reference(&self, _arguments: &Map<String, Value>) -> Result<Value, ToolExecutionError> {
        Err(ToolExecutionError::new(
            "SDK references are unavailable from the composed validator",
        ))
    }

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

/// Default loader that reads a JSON document from disk.
#[derive(Debug, Clone, Copy, Default)]
pub struct JsonFileConfigurationLoader;

impl ConfigurationLoader for JsonFileConfigurationLoader {
    fn load_json(&self, path: &str) -> Result<Value, String> {
        let text = std::fs::read_to_string(path).map_err(|error| error.to_string())?;
        serde_json::from_str(&text).map_err(|error| error.to_string())
    }
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
    /// Durable local job store, queried in `local` mode.
    pub job_store: Option<Arc<dyn JobStore>>,
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
            job_store: None,
            configuration_loader: Arc::new(JsonFileConfigurationLoader),
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
            Boundary::Local => {
                if self.runner.is_none()
                    && self.backtest_submitter.is_none()
                    && self.study_submitter.is_none()
                    && self.screening_submitter.is_none()
                    && self.job_store.is_none()
                {
                    return Err(ContextError(
                        "local execution requires a job store or a process runner".to_string(),
                    ));
                }
            }
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

type ToolHandler =
    Arc<dyn Fn(&Map<String, Value>) -> Result<Value, ToolExecutionError> + Send + Sync>;

/// One MCP tool: name, contract, and dispatch target.
pub struct Tool {
    /// Tool name.
    pub name: String,
    /// Human-readable description.
    pub description: String,
    /// JSON input schema.
    pub input_schema: Value,
    /// Capability that publishes this tool.
    pub capability: &'static str,
    read_only: bool,
    handler: ToolHandler,
}

impl Tool {
    /// Compose a tool from its contract and dispatch target.
    #[must_use]
    pub fn new<F>(
        name: &str,
        description: &str,
        input_schema: Value,
        capability: &'static str,
        handler: F,
    ) -> Self
    where
        F: Fn(&Map<String, Value>) -> Result<Value, ToolExecutionError> + Send + Sync + 'static,
    {
        Self {
            name: name.to_string(),
            description: description.to_string(),
            input_schema,
            capability,
            read_only: false,
            handler: Arc::new(handler),
        }
    }

    /// Declare a read operation at its registration boundary.
    #[must_use]
    pub fn read_only(mut self) -> Self {
        self.read_only = true;
        self
    }

    /// The `tools/list` entry for this tool.
    #[must_use]
    pub fn schema_entry(&self) -> Value {
        json!({
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {"readOnlyHint": self.read_only},
        })
    }

    /// Dispatch one tool call with already-validated arguments.
    ///
    /// # Errors
    ///
    /// Returns [`ToolExecutionError`] for domain-level failures.
    pub fn call(&self, arguments: &Map<String, Value>) -> Result<Value, ToolExecutionError> {
        (self.handler)(arguments)
    }
}

/// Immutable, capability-filtered tool registry.
pub struct ToolCatalog {
    tools: Vec<Tool>,
}

impl ToolCatalog {
    /// Compose a catalog, rejecting duplicate names.
    ///
    /// # Errors
    ///
    /// Returns [`ContextError`] when tool names are duplicated.
    pub fn new(tools: Vec<Tool>) -> Result<Self, ContextError> {
        let mut names: Vec<&str> = tools.iter().map(|tool| tool.name.as_str()).collect();
        names.sort_unstable();
        names.dedup();
        if names.len() != tools.len() {
            return Err(ContextError("tool names must be unique".to_string()));
        }
        Ok(Self { tools })
    }

    /// Return the named tool, or `None` when not published.
    #[must_use]
    pub fn resolve(&self, name: &str) -> Option<&Tool> {
        self.tools.iter().find(|tool| tool.name == name)
    }

    /// Return the published tool names in catalog order.
    #[must_use]
    pub fn names(&self) -> Vec<&str> {
        self.tools.iter().map(|tool| tool.name.as_str()).collect()
    }

    /// Return the published tools in catalog order.
    #[must_use]
    pub fn tools(&self) -> &[Tool] {
        &self.tools
    }
}

/// Handlers bound to one injected boundary composition.
pub struct HarnessTools {
    context: HarnessToolsContext,
}

impl HarnessTools {
    /// Bind handlers to one context.
    #[must_use]
    pub fn new(context: HarnessToolsContext) -> Self {
        Self { context }
    }

    /// Run one backtest through the selected local or remote boundary.
    ///
    /// # Errors
    ///
    /// Returns [`ToolExecutionError`] when the boundary fails.
    pub fn run_backtest(
        &self,
        arguments: &Map<String, Value>,
    ) -> Result<Value, ToolExecutionError> {
        let job_id = optional_identifier_argument(arguments, "job_id")?;
        let config_path = resolve_config(arguments, job_id.as_deref())?;
        if self.context.mode == Boundary::Remote {
            return self.launch_remote_backtest(&config_path, job_id.as_deref());
        }
        if let Some(submitter) = &self.context.backtest_submitter {
            let workspace = match arguments.get("workspace") {
                None | Some(Value::Null) => None,
                Some(Value::String(workspace)) if !workspace.trim().is_empty() => {
                    Some(workspace.clone())
                }
                Some(_) => {
                    return Err(ToolExecutionError::new(
                        "workspace must be a non-empty string",
                    ))
                }
            };
            return submitter
                .submit(&config_path, workspace.as_deref(), job_id.as_deref())
                .map_err(|error| {
                    ToolExecutionError::new(format!(
                        "the local backtest runner could not complete the backtest: {error}"
                    ))
                });
        }
        ensure_process_job_id(&config_path, job_id.as_deref())?;
        let mut tail = vec![
            "backtest-job".to_string(),
            "--config".to_string(),
            config_path,
        ];
        tail.extend(workspace_arguments(arguments)?);
        self.invoke_process(&tail, "backtest-job")
    }

    /// Run one study through the selected local or remote boundary.
    ///
    /// A study is a deterministic replay with no order execution: it fits and
    /// observes a research model and resolves indicators, publishing metrics
    /// without ever submitting orders. It is recorded as a study, never as a
    /// backtest.
    ///
    /// # Errors
    ///
    /// Returns [`ToolExecutionError`] when the boundary fails.
    pub fn run_study(&self, arguments: &Map<String, Value>) -> Result<Value, ToolExecutionError> {
        let job_id = optional_identifier_argument(arguments, "job_id")?;
        let config_path = resolve_config(arguments, job_id.as_deref())?;
        if self.context.mode == Boundary::Remote {
            return self.launch_remote_study(&config_path, job_id.as_deref());
        }
        if let Some(submitter) = &self.context.study_submitter {
            let workspace = workspace_option(arguments)?;
            return submitter
                .submit(&config_path, workspace.as_deref(), job_id.as_deref())
                .map_err(|error| {
                    ToolExecutionError::new(format!(
                        "the local study runner could not complete the study: {error}"
                    ))
                });
        }
        ensure_process_job_id(&config_path, job_id.as_deref())?;
        let mut tail = vec!["study-job".to_string(), "--config".to_string(), config_path];
        tail.extend(workspace_arguments(arguments)?);
        self.invoke_process(&tail, "study-job")
    }

    /// Launch one screening run through the selected local or remote boundary.
    ///
    /// A screening run evaluates one JIT-authored cross-sectional program and
    /// produces a ranked `screen.json` artifact. The local boundary returns the
    /// job id immediately and completes the run in the background; the
    /// remote boundary posts the configuration to the screener launch route.
    ///
    /// # Errors
    ///
    /// Returns [`ToolExecutionError`] when the boundary fails.
    pub fn run_screener(
        &self,
        arguments: &Map<String, Value>,
    ) -> Result<Value, ToolExecutionError> {
        let job_id = optional_identifier_argument(arguments, "job_id")?;
        let config_path = resolve_config(arguments, job_id.as_deref())?;
        if self.context.mode == Boundary::Remote {
            return self.launch_remote_screener(&config_path, job_id.as_deref());
        }
        if let Some(submitter) = &self.context.screening_submitter {
            let workspace = workspace_option(arguments)?;
            return submitter
                .submit(&config_path, workspace.as_deref(), job_id.as_deref())
                .map_err(|error| {
                    ToolExecutionError::new(format!(
                        "the local screening runner could not complete the screening run: {error}"
                    ))
                });
        }
        ensure_process_job_id(&config_path, job_id.as_deref())?;
        let mut tail = vec![
            "screen-job".to_string(),
            "--config".to_string(),
            config_path,
        ];
        tail.extend(workspace_arguments(arguments)?);
        self.invoke_process(&tail, "screen-job")
    }

    /// Validate one authored model document without replaying history.
    ///
    /// The model's program is compiled, loaded, and constructed exactly as a
    /// run would, so a source that cannot build or omits the SDK handshake
    /// exports fails here while the caller can still repair it. The model
    /// document is the same object the completion contract carries; the
    /// platform builds the runtime study job from it internally.
    ///
    /// # Errors
    ///
    /// Returns [`ToolExecutionError`] when the model document is malformed or
    /// the authored program fails to build.
    pub fn validate_study(
        &self,
        arguments: &Map<String, Value>,
    ) -> Result<Value, ToolExecutionError> {
        let Some(model) = arguments.get("model") else {
            return Err(ToolExecutionError::new(
                "validate_study requires the model object under 'model'",
            ));
        };
        if self.context.mode == Boundary::Remote {
            return self.remote_validation(TOOL_VALIDATE_STUDY, model);
        }
        let Some(validator) = &self.context.study_validator else {
            return Err(ToolExecutionError::new(
                "study validation requires a local study validator in this MCP server",
            ));
        };
        validator.validate_result(model)
    }

    /// Validate a strategy through the same injected native compiler boundary.
    pub fn validate_strategy(
        &self,
        arguments: &Map<String, Value>,
    ) -> Result<Value, ToolExecutionError> {
        let model = arguments.get("model").ok_or_else(|| {
            ToolExecutionError::new("validate_strategy requires the model object under 'model'")
        })?;
        if self.context.mode == Boundary::Remote {
            return self.remote_validation(TOOL_VALIDATE_STRATEGY, model);
        }
        self.context
            .study_validator
            .as_ref()
            .ok_or_else(|| {
                ToolExecutionError::new(
                    "strategy validation requires a local program validator in this MCP server",
                )
            })?
            .validate_strategy_result(model)
    }

    fn remote_validation(&self, name: &str, model: &Value) -> Result<Value, ToolExecutionError> {
        self.remote_tool(name, &json!({"model": model}))
    }

    fn remote_tool(&self, name: &str, arguments: &Value) -> Result<Value, ToolExecutionError> {
        let url = self.remote_url("/mcp")?;
        let response = self.dispatch_remote_post(
            &url,
            &json!({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": arguments}
            }),
        )?;
        if matches!(response.status_code, 401 | 403) {
            return Err(remote_response_failure(
                &response,
                "remote credential rejected",
            ));
        }
        if !(200..300).contains(&response.status_code) {
            return Err(remote_response_failure(
                &response,
                &format!("remote {name} failed"),
            ));
        }
        let body = response.body;
        if body["jsonrpc"] != "2.0" || body["id"] != 1 {
            return Err(ToolExecutionError::new(
                "remote validation returned an invalid JSON-RPC envelope",
            )
            .with_certainty("unknown_outcome"));
        }
        if let Some(error) = body.get("error") {
            let certainty = error
                .get("data")
                .and_then(|data| data.get("executionCertainty"))
                .and_then(Value::as_str)
                .filter(|value| {
                    matches!(
                        *value,
                        "not_started" | "observed_failure" | "unknown_outcome"
                    )
                })
                .unwrap_or(
                    if matches!(
                        error["code"].as_i64(),
                        Some(-32700 | -32600 | -32601 | -32602)
                    ) {
                        "not_started"
                    } else {
                        "unknown_outcome"
                    },
                );
            return Err(ToolExecutionError::with_payload(
                error["message"]
                    .as_str()
                    .unwrap_or("remote validation protocol failure"),
                json!({"serverError": error}),
            )
            .with_certainty(certainty));
        }
        let result = &body["result"];
        let payload = &result["structuredContent"];
        if result["isError"] == true {
            let text = result["content"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(|block| block["text"].as_str())
                .collect::<Vec<_>>()
                .join("\n");
            return Err(ToolExecutionError::with_payload(
                if text.is_empty() {
                    "remote validation failed".to_string()
                } else {
                    text
                },
                payload.clone(),
            ));
        }
        if result["isError"] != false
            || !payload.is_object()
            || (name != TOOL_READ_SDK_REFERENCE && payload["valid"] != true)
        {
            return Err(ToolExecutionError::new(
                "remote validation returned no valid structured outcome",
            )
            .with_certainty("unknown_outcome"));
        }
        Ok(payload.clone())
    }

    /// Read metadata or a bounded source range from the serving compiler's SDK.
    pub fn read_sdk_reference(
        &self,
        arguments: &Map<String, Value>,
    ) -> Result<Value, ToolExecutionError> {
        if self.context.mode == Boundary::Remote {
            return self.remote_tool(TOOL_READ_SDK_REFERENCE, &Value::Object(arguments.clone()));
        }
        self.context
            .study_validator
            .as_ref()
            .ok_or_else(|| {
                ToolExecutionError::new("SDK references require a native program validator")
            })?
            .sdk_reference(arguments)
    }

    /// Return the embedded starter backtest configuration document.
    ///
    /// # Errors
    ///
    /// Returns [`ToolExecutionError`] when the template cannot be serialized.
    pub fn backtest_config_template(&self) -> Result<Value, ToolExecutionError> {
        Ok(json!({
            "format": "yaml",
            "template": BACKTEST_CONFIG_TEMPLATE,
        }))
    }

    /// Return the embedded starter screening configuration document.
    ///
    /// The template is a complete `shell: screen` document with an inline
    /// example program using `export_screening_program!`; its embedded
    /// `bundle_hash` matches the example source, and an edited source requires
    /// recomputing that hash.
    ///
    /// # Errors
    ///
    /// Returns [`ToolExecutionError`] when the template cannot be serialized.
    pub fn screener_config_template(&self) -> Result<Value, ToolExecutionError> {
        Ok(json!({
            "format": "yaml",
            "template": SCREENER_CONFIG_TEMPLATE,
        }))
    }

    /// Run one supervised paper trading session locally.
    ///
    /// # Errors
    ///
    /// Returns [`ToolExecutionError`] when the process boundary fails.
    pub fn launch_trader(
        &self,
        arguments: &Map<String, Value>,
    ) -> Result<Value, ToolExecutionError> {
        let config_path = text_argument(arguments, "config_path")?;
        self.invoke_process(
            &["trader".to_string(), "--config".to_string(), config_path],
            "trader",
        )
    }

    /// Read one job's persisted state from the remote harness API.
    ///
    /// # Errors
    ///
    /// Returns [`ToolExecutionError`] when the remote call fails.
    pub fn get_job_status(
        &self,
        arguments: &Map<String, Value>,
    ) -> Result<Value, ToolExecutionError> {
        let job_id = identifier_argument(arguments, "job_id")?;
        if self.context.mode == Boundary::Local {
            return self.local_job(&job_id);
        }
        let url = self.remote_url(&format!(
            "{JOB_ROUTE}/{}",
            utf8_percent_encode(&job_id, PATH_SEGMENT)
        ))?;
        self.remote_get(&url, &format!("job '{job_id}' does not exist"))
    }

    /// Read one stored job result from the remote harness API.
    ///
    /// # Errors
    ///
    /// Returns [`ToolExecutionError`] when the remote call fails.
    pub fn get_job_result(
        &self,
        arguments: &Map<String, Value>,
    ) -> Result<Value, ToolExecutionError> {
        let job_id = identifier_argument(arguments, "job_id")?;
        if self.context.mode == Boundary::Local {
            return self.local_job(&job_id);
        }
        let url = self.remote_url(&format!(
            "{JOB_ROUTE}/{}",
            utf8_percent_encode(&job_id, PATH_SEGMENT)
        ))?;
        self.remote_get(&url, &format!("job '{job_id}' does not exist"))
    }

    /// List one stored job's artifacts from the remote harness API.
    ///
    /// # Errors
    ///
    /// Returns [`ToolExecutionError`] when the remote call fails.
    pub fn list_artifacts(
        &self,
        arguments: &Map<String, Value>,
    ) -> Result<Value, ToolExecutionError> {
        let job_id = identifier_argument(arguments, "job_id")?;
        if self.context.mode == Boundary::Local {
            return self.local_artifacts(&job_id);
        }
        let url = self.remote_url(&format!(
            "{JOB_ROUTE}/{}/artifacts",
            utf8_percent_encode(&job_id, PATH_SEGMENT)
        ))?;
        self.remote_get(&url, &format!("job '{job_id}' does not exist"))
    }

    /// Read one stored job artifact's contents as text or hex-encoded bytes.
    ///
    /// # Errors
    ///
    /// Returns [`ToolExecutionError`] when the job or artifact does not exist,
    /// or the authenticated remote artifact response has mismatched attribution.
    pub fn read_artifact(
        &self,
        arguments: &Map<String, Value>,
    ) -> Result<Value, ToolExecutionError> {
        let job_id = identifier_argument(arguments, "job_id")?;
        let name = text_argument(arguments, "name")?;
        if self.context.mode == Boundary::Local {
            return self.local_artifact(&job_id, &name);
        }
        let path = name
            .split('/')
            .map(|part| utf8_percent_encode(part, PATH_SEGMENT).to_string())
            .collect::<Vec<_>>()
            .join("/");
        let url = self.remote_url(&format!(
            "{JOB_ROUTE}/{}/artifacts/{path}?representation=json",
            utf8_percent_encode(&job_id, PATH_SEGMENT)
        ))?;
        let document = self.remote_get(
            &url,
            &format!("artifact '{name}' does not exist for job '{job_id}'"),
        )?;
        if document.get("job_id").and_then(Value::as_str) != Some(job_id.as_str())
            || document.get("name").and_then(Value::as_str) != Some(name.as_str())
        {
            return Err(ToolExecutionError::new(
                "remote artifact response attribution does not match request",
            ));
        }
        Ok(document)
    }

    fn local_job(&self, job_id: &str) -> Result<Value, ToolExecutionError> {
        let store = self.context.job_store.as_ref().ok_or_else(|| {
            ToolExecutionError::new("local jobs require a durable job store in this MCP server")
        })?;
        match store.get(job_id) {
            Some(record) => serde_json::to_value(&record)
                .map_err(|error| ToolExecutionError::new(error.to_string())),
            None => Err(ToolExecutionError::new(format!(
                "job '{job_id}' does not exist"
            ))),
        }
    }

    fn local_artifacts(&self, job_id: &str) -> Result<Value, ToolExecutionError> {
        let store = self.context.job_store.as_ref().ok_or_else(|| {
            ToolExecutionError::new("local jobs require a durable job store in this MCP server")
        })?;
        let stored = store
            .fetch(job_id)
            .ok_or_else(|| ToolExecutionError::new(format!("job '{job_id}' does not exist")))?;
        let prefix = artifact_prefix(&stored.record.job_id)
            .map_err(|error| ToolExecutionError::new(error.to_string()))?;
        let artifacts: Vec<Value> = stored
            .artifacts
            .iter()
            .map(|(name, payload)| {
                let uri = format!("{prefix}/{name}");
                json!({
                    "name": name,
                    "uri": uri,
                    "sha256": artifact_digest(payload),
                    "sizeBytes": payload.len() as u64,
                    "sas_url": Value::Null,
                })
            })
            .collect();
        Ok(json!({"job_id": stored.record.job_id, "artifacts": artifacts}))
    }

    fn local_artifact(&self, job_id: &str, name: &str) -> Result<Value, ToolExecutionError> {
        let store = self.context.job_store.as_ref().ok_or_else(|| {
            ToolExecutionError::new("local jobs require a durable job store in this MCP server")
        })?;
        let stored = store
            .fetch(job_id)
            .ok_or_else(|| ToolExecutionError::new(format!("job '{job_id}' does not exist")))?;
        let payload = stored.artifacts.get(name).ok_or_else(|| {
            ToolExecutionError::new(format!(
                "artifact '{name}' does not exist for job '{job_id}'"
            ))
        })?;
        let document = match std::str::from_utf8(payload) {
            Ok(content) => json!({
                "job_id": job_id,
                "name": name,
                "encoding": "utf-8",
                "content": content,
            }),
            Err(_) => json!({
                "job_id": job_id,
                "name": name,
                "encoding": "hex",
                "content": hex::encode(payload),
            }),
        };
        Ok(document)
    }

    fn invoke_process(&self, tail: &[String], process: &str) -> Result<Value, ToolExecutionError> {
        let Some(runner) = &self.context.runner else {
            return Err(ToolExecutionError::new(format!(
                "the local harness process '{process}' could not be invoked: \
                 no process runner is composed"
            )));
        };
        let mut argv = self.context.program.clone();
        argv.extend(tail.iter().cloned());
        let outcome = runner.run(&argv).map_err(|_| {
            ToolExecutionError::new(format!(
                "the local harness process '{process}' could not be invoked"
            ))
        })?;
        let payload = json!({
            "boundary": "local",
            "exit_code": outcome.returncode,
            "stdout": outcome.stdout,
            "stderr": outcome.stderr,
        });
        if outcome.returncode != 0 {
            return Err(ToolExecutionError::with_payload(
                format!(
                    "the local harness process '{process}' exited with code {}",
                    outcome.returncode
                ),
                payload,
            ));
        }
        Ok(payload)
    }

    fn launch_remote_backtest(
        &self,
        config_path: &str,
        job_id: Option<&str>,
    ) -> Result<Value, ToolExecutionError> {
        let mut payload = self
            .context
            .configuration_loader
            .load_json(config_path)
            .map_err(|error| {
                ToolExecutionError::new(format!(
                    "could not load the backtest configuration '{config_path}': {error}"
                ))
            })?;
        backtest::backtest::job::overlay_job_id_json(&mut payload, job_id)
            .map_err(|error| ToolExecutionError::new(error.to_string()))?;
        let url = self.remote_url(LAUNCH_ROUTE)?;
        validate_configuration(&payload, std::path::Path::new(config_path).parent())?;
        let response = self.dispatch_remote_post(&url, &payload)?;
        let status_code = response.status_code;
        if status_code == 401 || status_code == 403 {
            return Err(remote_response_failure(
                &response,
                "remote credential rejected",
            ));
        }
        if status_code == 404 || status_code == 501 {
            return Err(remote_response_failure(
                &response,
                "the remote harness API does not implement backtest launch",
            ));
        }
        if !(200..300).contains(&status_code) {
            return Err(remote_response_failure(
                &response,
                "remote backtest launch failed",
            ));
        }
        let job_id = response
            .body
            .get("job_id")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| {
                ToolExecutionError::with_payload(
                    "remote harness launch response requires a non-empty 'job_id' string",
                    response.body.clone(),
                )
                .with_certainty("unknown_outcome")
            })?;
        let state = response
            .body
            .get("state")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| {
                ToolExecutionError::with_payload(
                    "remote harness launch response requires a non-empty 'state' string",
                    response.body.clone(),
                )
                .with_certainty("unknown_outcome")
            })?;
        Ok(json!({
            "boundary": "remote",
            "job_id": job_id,
            "state": state,
            "kind": "backtest",
        }))
    }

    fn launch_remote_study(
        &self,
        config_path: &str,
        job_id: Option<&str>,
    ) -> Result<Value, ToolExecutionError> {
        let mut payload = self
            .context
            .configuration_loader
            .load_json(config_path)
            .map_err(|error| {
                ToolExecutionError::new(format!(
                    "could not load the study configuration '{config_path}': {error}"
                ))
            })?;
        backtest::backtest::job::overlay_job_id_json(&mut payload, job_id)
            .map_err(|error| ToolExecutionError::new(error.to_string()))?;
        let url = self.remote_url(STUDY_LAUNCH_ROUTE)?;
        validate_configuration(&payload, std::path::Path::new(config_path).parent())?;
        let response = self.dispatch_remote_post(&url, &payload)?;
        let status_code = response.status_code;
        if status_code == 401 || status_code == 403 {
            return Err(remote_response_failure(
                &response,
                "remote credential rejected",
            ));
        }
        if status_code == 404 || status_code == 501 {
            return Err(remote_response_failure(
                &response,
                "the remote harness API does not implement study launch",
            ));
        }
        if !(200..300).contains(&status_code) {
            return Err(remote_response_failure(
                &response,
                "remote study launch failed",
            ));
        }
        let job_id = response
            .body
            .get("job_id")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| {
                ToolExecutionError::with_payload(
                    "remote harness study response requires a non-empty 'job_id' string",
                    response.body.clone(),
                )
                .with_certainty("unknown_outcome")
            })?;
        let state = response
            .body
            .get("state")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| {
                ToolExecutionError::with_payload(
                    "remote harness study response requires a non-empty 'state' string",
                    response.body.clone(),
                )
                .with_certainty("unknown_outcome")
            })?;
        Ok(json!({
            "boundary": "remote",
            "job_id": job_id,
            "state": state,
            "kind": "study",
        }))
    }

    fn launch_remote_screener(
        &self,
        config_path: &str,
        job_id: Option<&str>,
    ) -> Result<Value, ToolExecutionError> {
        let mut payload = self
            .context
            .configuration_loader
            .load_json(config_path)
            .map_err(|error| {
                ToolExecutionError::new(format!(
                    "could not load the screening configuration '{config_path}': {error}"
                ))
            })?;
        backtest::backtest::job::overlay_job_id_json(&mut payload, job_id)
            .map_err(|error| ToolExecutionError::new(error.to_string()))?;
        let url = self.remote_url(SCREENER_LAUNCH_ROUTE)?;
        validate_configuration(&payload, std::path::Path::new(config_path).parent())?;
        let response = self.dispatch_remote_post(&url, &payload)?;
        let status_code = response.status_code;
        if status_code == 401 || status_code == 403 {
            return Err(remote_response_failure(
                &response,
                "remote credential rejected",
            ));
        }
        if status_code == 404 || status_code == 501 {
            return Err(remote_response_failure(
                &response,
                "the remote harness API does not implement screening launch",
            ));
        }
        if !(200..300).contains(&status_code) {
            return Err(remote_response_failure(
                &response,
                "remote screening launch failed",
            ));
        }
        let job_id = response
            .body
            .get("job_id")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| {
                ToolExecutionError::with_payload(
                    "remote harness screener response requires a non-empty 'job_id' string",
                    response.body.clone(),
                )
                .with_certainty("unknown_outcome")
            })?;
        let state = response
            .body
            .get("state")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| {
                ToolExecutionError::with_payload(
                    "remote harness screener response requires a non-empty 'state' string",
                    response.body.clone(),
                )
                .with_certainty("unknown_outcome")
            })?;
        Ok(json!({
            "boundary": "remote",
            "job_id": job_id,
            "state": state,
            "kind": "screen",
        }))
    }

    fn remote_get(&self, url: &str, not_found_message: &str) -> Result<Value, ToolExecutionError> {
        let response = self.dispatch_remote_get(url)?;
        match response.status_code {
            200 => Ok(response.body),
            401 | 403 => Err(credential_rejected(response.status_code)),
            404 => Err(ToolExecutionError::new(not_found_message)),
            other => Err(ToolExecutionError::new(format!(
                "the remote harness API returned HTTP {other}"
            ))),
        }
    }

    fn dispatch_remote_get(&self, url: &str) -> Result<RemoteResponse, ToolExecutionError> {
        let headers = self.remote_headers()?;
        let transport = self
            .context
            .remote_transport
            .as_ref()
            .ok_or_else(remote_unconfigured)?;
        transport
            .get(url, &headers)
            .map_err(|_| ToolExecutionError::new("the remote harness API could not be reached"))
    }

    fn dispatch_remote_post(
        &self,
        url: &str,
        payload: &Value,
    ) -> Result<RemoteResponse, ToolExecutionError> {
        let headers = self.remote_headers()?;
        let transport = self
            .context
            .remote_transport
            .as_ref()
            .ok_or_else(remote_unconfigured)?;
        transport.post(url, &headers, payload).map_err(|_| {
            ToolExecutionError::new(
                "the remote harness API could not be reached; request outcome is unknown",
            )
            .with_certainty("unknown_outcome")
        })
    }

    fn remote_headers(&self) -> Result<BTreeMap<String, String>, ToolExecutionError> {
        self.context.credentials.bearer_headers().map_err(|error| {
            ToolExecutionError::new(format!(
                "remote harness API calls need a bearer credential: {error}"
            ))
            .with_certainty("not_started")
        })
    }

    fn remote_url(&self, route: &str) -> Result<String, ToolExecutionError> {
        let base_url = self
            .context
            .api_base_url
            .as_ref()
            .ok_or_else(remote_unconfigured)?;
        Ok(format!("{base_url}{route}"))
    }
}

fn remote_unconfigured() -> ToolExecutionError {
    ToolExecutionError::new("the remote harness API boundary is not configured for this MCP server")
        .with_certainty("not_started")
}

fn credential_rejected(status_code: u16) -> ToolExecutionError {
    ToolExecutionError::new(format!(
        "the remote harness API rejected the bearer credential (HTTP {status_code})"
    ))
    .with_certainty("not_started")
}

fn remote_response_failure(response: &RemoteResponse, message: &str) -> ToolExecutionError {
    let certainty = response
        .body
        .get("executionCertainty")
        .or_else(|| {
            response
                .body
                .get("error")
                .and_then(|error| error.get("executionCertainty"))
        })
        .and_then(Value::as_str)
        .filter(|value| {
            matches!(
                *value,
                "not_started" | "observed_failure" | "unknown_outcome"
            )
        })
        .unwrap_or(
            if (400..500).contains(&response.status_code) || response.status_code == 501 {
                "not_started"
            } else {
                "unknown_outcome"
            },
        );
    let detail = response
        .body
        .get("error")
        .and_then(|error| error.get("message"))
        .or_else(|| response.body.get("message"))
        .and_then(Value::as_str);
    ToolExecutionError::with_payload(
        format!("{message} (HTTP {}){}", response.status_code, detail.map(|detail| format!(": {detail}")).unwrap_or_default()),
        json!({"boundary": "remote", "statusCode": response.status_code, "serverError": response.body}),
    ).with_certainty(certainty)
}

fn text_argument(arguments: &Map<String, Value>, key: &str) -> Result<String, ToolExecutionError> {
    arguments
        .get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string)
        .ok_or_else(|| ToolExecutionError::new(format!("{key} must be a non-empty string")))
}

fn identifier_argument(
    arguments: &Map<String, Value>,
    key: &str,
) -> Result<String, ToolExecutionError> {
    let value = text_argument(arguments, key)?;
    if value.len() > IDENTIFIER_MAX {
        return Err(ToolExecutionError::new(format!(
            "{key} must be at most {IDENTIFIER_MAX} characters"
        )));
    }
    Ok(value)
}

fn optional_identifier_argument(
    arguments: &Map<String, Value>,
    key: &str,
) -> Result<Option<String>, ToolExecutionError> {
    match arguments.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(_) => identifier_argument(arguments, key).map(Some),
    }
}

fn ensure_process_job_id(
    config_path: &str,
    job_id: Option<&str>,
) -> Result<(), ToolExecutionError> {
    let Some(job_id) = job_id else {
        return Ok(());
    };
    let text = std::fs::read_to_string(config_path).map_err(|error| {
        ToolExecutionError::new(format!(
            "could not read the configuration '{config_path}': {error}"
        ))
    })?;
    let value = backtest::backtest::yaml::load_yaml(&text).map_err(|error| {
        ToolExecutionError::new(format!(
            "could not parse the configuration '{config_path}': {}",
            error.0
        ))
    })?;
    let declared = match &value {
        backtest::backtest::yaml::YamlValue::Mapping(mapping) => mapping
            .get("job_id")
            .and_then(backtest::backtest::yaml::YamlValue::as_str),
        _ => None,
    };
    if declared.map(str::trim) != Some(job_id.trim()) {
        return Err(ToolExecutionError::new(
            "the local process boundary cannot set job_id on an existing configuration \
             document; add the id to the document or compose a local submitter",
        ));
    }
    Ok(())
}

/// Resolve the backtest configuration path from an existing path or an inline
/// ``config`` document.
///
/// An inline document is serialized and written to a content-addressed file
/// under the temp directory so the asynchronous runner can read it after the
/// tool call returns; identical documents reuse the same file.
fn resolve_config(
    arguments: &Map<String, Value>,
    job_id: Option<&str>,
) -> Result<String, ToolExecutionError> {
    if let Some(value) = arguments.get("config") {
        if !value.is_null() {
            let Some(document) = value.as_object() else {
                return Err(ToolExecutionError::new("config must be a JSON object"));
            };
            let mut document = Value::Object(document.clone());
            backtest::backtest::job::overlay_job_id_json(&mut document, job_id)
                .map_err(|error| ToolExecutionError::new(error.to_string()))?;
            validate_configuration(&document, None)?;
            let text = serde_json::to_string(&document)
                .map_err(|error| ToolExecutionError::new(error.to_string()))?;
            return materialize_config(&text);
        }
    }
    text_argument(arguments, "config_path")
}

/// Validate with the same replay and screening schemas used by API and engine.
pub fn validate_configuration(
    document: &Value,
    base: Option<&std::path::Path>,
) -> Result<(), ToolExecutionError> {
    if document.get("shell").and_then(Value::as_str) == Some("screen") {
        backtest::backtest::screen::ScreenConfig::from_json_value_with_base(document, base)
            .map_err(|e| ToolExecutionError::new(e.to_string()))?;
    } else {
        backtest::backtest::config::BacktestConfig::from_yaml(&document.to_string())
            .map_err(|e| ToolExecutionError::new(e.to_string()))?;
    }
    Ok(())
}

fn materialize_config(text: &str) -> Result<String, ToolExecutionError> {
    let digest = hex::encode(Sha256::digest(text.as_bytes()));
    let directory = std::env::temp_dir().join("harness-mcp-configs");
    std::fs::create_dir_all(&directory).map_err(|error| {
        ToolExecutionError::new(format!("could not prepare the config directory: {error}"))
    })?;
    let path = directory.join(format!("{digest}.json"));
    if !path.exists() {
        std::fs::write(&path, text).map_err(|error| {
            ToolExecutionError::new(format!(
                "could not write the inline backtest config: {error}"
            ))
        })?;
    }
    Ok(path.to_string_lossy().into_owned())
}

fn workspace_arguments(arguments: &Map<String, Value>) -> Result<Vec<String>, ToolExecutionError> {
    match arguments.get("workspace") {
        None | Some(Value::Null) => Ok(Vec::new()),
        Some(Value::String(workspace)) if !workspace.trim().is_empty() => {
            Ok(vec!["--output-dir".to_string(), workspace.clone()])
        }
        Some(_) => Err(ToolExecutionError::new(
            "workspace must be a non-empty string",
        )),
    }
}

fn text_property() -> Value {
    json!({"type": "string", "minLength": 1, "maxLength": IDENTIFIER_MAX})
}

fn workspace_option(arguments: &Map<String, Value>) -> Result<Option<String>, ToolExecutionError> {
    match arguments.get("workspace") {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(workspace)) if !workspace.trim().is_empty() => {
            Ok(Some(workspace.clone()))
        }
        Some(_) => Err(ToolExecutionError::new(
            "workspace must be a non-empty string",
        )),
    }
}

fn study_subscription_schema() -> Value {
    let symbols = json!({"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1, "pattern": "^\\S(?:.*\\S)?$"}});
    json!({
        "type": "array", "minItems": 1,
        "description": "Exact requested inputs, matching the program and its indicators. Bars support multiple session-anchored minute/hour intervals and 1d; quote/trade must omit interval. Symbol aliases must equal the subscription symbol union; duplicate aliases must agree. Symbols must be nonempty and unpadded.",
        "items": {"oneOf": [
            {"type": "object", "additionalProperties": false,
             "required": ["data_type", "symbols", "interval"],
             "properties": {"data_type": {"const": "bars"}, "symbols": symbols,
                            "interval": {"type": "string", "pattern": "^(?:[1-9][0-9]*(?:min|h)|1d)$"}}},
            {"type": "object", "additionalProperties": false,
             "required": ["data_type", "symbols"],
             "properties": {"data_type": {"type": "string", "enum": ["quote", "trade"]}, "symbols": symbols}}
        ]}
    })
}

fn study_configuration_schema() -> Value {
    let symbols =
        json!({"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}});
    let data = json!({
        "type": "object", "additionalProperties": false,
        "required": ["subscriptions"],
        "properties": {
            "snapshot_id": {"type": "string", "minLength": 1},
            "fingerprint": {"type": "string"},
            "subscriptions": study_subscription_schema(),
            "symbols": symbols
        }
    });
    json!({
        "type": "object",
        "required": ["start", "end"],
        "anyOf": [
            {"required": ["subscriptions"]},
            {"required": ["data"]}
        ],
        "description": "Supported replay configuration. Declare exact subscriptions; bars support multiple completed minute/hour intervals and 1d, tick-only inputs omit interval, and symbol aliases must equal the subscription union.",
        "properties": {
            "start": {"type": "string", "format": "date-time", "pattern": "(Z|[+-][0-9]{2}:[0-9]{2})$", "description": "RFC 3339 timestamp with Z or explicit UTC offset; must precede end."},
            "end": {"type": "string", "format": "date-time", "pattern": "(Z|[+-][0-9]{2}:[0-9]{2})$", "description": "RFC 3339 timestamp with Z or explicit UTC offset; must follow start."},
            "subscriptions": study_subscription_schema(),
            "requiredSymbols": symbols,
            "required_symbols": symbols,
            "symbols": symbols,
            "data": data
        }
    })
}

fn authored_model_schema() -> Value {
    json!({
        "type": "object", "required": ["source", "harnessConfiguration"],
        "properties": {
            "modelId": {"type": "string", "minLength": 1, "maxLength": 128},
            "version": {"type": "string", "minLength": 1, "maxLength": 64},
            "description": {"type": "string", "minLength": 1},
            "source": {"type": "string", "minLength": 1},
            "outputs": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
            "tests": {"type": "object"},
            "harnessConfiguration": study_configuration_schema()
        }
    })
}

/// Build the capability-filtered tool catalog for one composition.
///
/// # Errors
///
/// Returns [`ContextError`] when the catalog cannot be composed.
pub fn build_catalog(context: HarnessToolsContext) -> Result<ToolCatalog, ContextError> {
    let capabilities = context.capabilities;
    let tools = Arc::new(HarnessTools::new(context));
    let mut candidates: Vec<Tool> = Vec::new();
    if capabilities.study || capabilities.backtest {
        let handler = tools.clone();
        candidates.push(Tool::new(
            TOOL_READ_SDK_REFERENCE,
            "Read the serving harness compiler's SDK identity/version/ABI and source hashes. \
             Omit topic for metadata only; topic reads bounded current SDK source, not a local \
             package or stale template. Compare sdkIdentity before using local SDK guidance. \
             Refresh initialize/serverInfo to compare current validationIdentity before running.",
            json!({"type": "object", "additionalProperties": false, "properties": {
                "topic": {"type": "string", "enum": ["consumer", "program", "indicator_api", "wavelet", "events", "exports", "decimal", "building_blocks", "outputs", "research_views", "research_publishing", "research_recipes"]},
                "start_line": {"type": "integer", "minimum": 1}, "end_line": {"type": "integer", "minimum": 1},
                "max_chars": {"type": "integer", "minimum": 1, "maximum": 16000}
            }}), "sdk", move |arguments| handler.read_sdk_reference(arguments),
        ).read_only());
    }

    if capabilities.backtest {
        let handler = tools.clone();
        candidates.push(Tool::new(
            TOOL_RUN_BACKTEST,
            "Run one deterministic backtest: a local 'harness backtest-job' process, \
             or a bearer-authenticated POST to the remote harness API returning {job_id, state, kind}. \
             config_path must reference an existing, readable configuration document; \
             do not guess paths.",
            json!({
                "type": "object",
                "additionalProperties": false,
                "anyOf": [
                    {"required": ["config_path"]},
                    {"required": ["config"]}
                ],
                "properties": {
                    "config_path": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Path to an existing backtest configuration document."
                    },
                    "config": {
                        "type": "object",
                        "description": "An inline backtest configuration document, used when no config file exists. Same schema as the configuration file; call backtest_config_template for a starter document."
                    },
                    "job_id": text_property(),
                    "workspace": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Local output directory passed as --output-dir; ignored when the remote API executes the launch."
                    }
                }
            }),
            "backtest",
            move |arguments| handler.run_backtest(arguments),
        ));
    }
    if capabilities.study {
        let handler = tools.clone();
        candidates.push(Tool::new(
            TOOL_RUN_STUDY,
            "Run one deterministic study: a local 'harness study-job' process, or a \
             bearer-authenticated POST to the remote harness API returning {job_id, state, kind}. \
             A study replays history to fit and observe a research model and resolve \
             indicators with no order execution; it is never recorded as a backtest. \
             config_path must reference an existing, readable study configuration document; \
             do not guess paths.",
            json!({
                "type": "object",
                "additionalProperties": false,
                "anyOf": [
                    {"required": ["config_path"]},
                    {"required": ["config"]}
                ],
                "properties": {
                    "config_path": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Path to an existing study configuration document."
                    },
                    "config": {
                        "type": "object",
                        "description": "An inline study configuration document, used when no config file exists."
                    },
                    "job_id": text_property(),
                    "workspace": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Local output directory passed as --output-dir; ignored when the remote API executes the launch."
                    }
                }
            }),
            "study",
            move |arguments| handler.run_study(arguments),
        ));
        let handler = tools.clone();
        candidates.push(Tool::new(
            TOOL_VALIDATE_STUDY,
            "Validate the study model you are drafting without replaying \
             history: compile, load, and construct its authored Rust program \
             over the managed JIT workspace and return compact structured \
             diagnostics. Pass the model object itself under 'model' - the same \
             object the completion contract carries. model.source and \
             model.harnessConfiguration.start/.end are required RFC 3339 timestamps \
             with Z or an explicit offset (for example 2025-01-01T00:00:00-05:00), \
             with start before end. Declare exact subscriptions directly or under \
             data (data.subscriptions); interval-only configuration is rejected. \
             Use harness_strategy_sdk imports and its serde_json::Value re-export. \
             Implement MarketConsumer with type Context = ObservationContext and \
             fn subscriptions(&self) -> Vec<Subscription>; use Subscription::Bars \
             { symbols, interval }, Subscription::Quote { symbols }, or \
             Subscription::Trade { symbols }. Put market callbacks on MarketConsumer: \
             fn on_bar(&mut self, frame: &BarFrame, ctx: &mut ObservationContext), \
             on_quote(&mut self, event: &QuoteEvent, ctx: &mut ObservationContext), \
             on_trade(&mut self, event: &TradeEvent, ctx: &mut ObservationContext). \
             BarFrame has bars_by_symbol and missing_symbols, not a single bar. \
             Decimal fields are exact; use checked arithmetic, not primitive casts. \
             Implement StudyProgram::new(parameters: Value) -> Self and optionally \
             on_stop(&mut self, metrics: &mut MetricPublisher); metrics.publish(name, \
             value: f64) returns Result<(), PublishError>. Export at module scope \
             with harness_strategy_sdk::export_study_program!(MyStudy). A study has \
             no order sink. Keep subscriptions and indicator topology stable; \
             indicators update before their consumer. Validation does not replay \
             data or prove output behavior. Fix reported source/configuration \
             errors and call validate_study again before submitting the contract; \
             report infrastructure failures rather than treating them as source errors.",
            json!({
                "type": "object",
                "additionalProperties": false,
                "required": ["model"],
                "properties": {
                    "model": authored_model_schema()
                }
            }),
            "study",
            move |arguments| handler.validate_study(arguments),
        ));
    }
    if capabilities.screener {
        let handler = tools.clone();
        candidates.push(Tool::new(
            TOOL_RUN_SCREENER,
            "Run one deterministic screening run: a local 'harness screen-job' process, \
             or a bearer-authenticated POST to the remote harness API returning \
             {job_id, state, kind}. A screening run evaluates one JIT-authored \
             cross-sectional Rust program and produces a ranked 'screen.json' artifact; \
             market data arrives only through the harness data-source port, so an \
             unavailable lake fails the run loudly. config_path must reference an existing, \
             readable screen configuration document; do not guess paths. Use \
             screener_config_template for a starter document.",
            json!({
                "type": "object",
                "additionalProperties": false,
                "anyOf": [
                    {"required": ["config_path"]},
                    {"required": ["config"]}
                ],
                "properties": {
                    "config_path": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Path to an existing screen configuration document."
                    },
                    "config": {
                        "type": "object",
                        "description": "An inline screen configuration document, used when no config file exists. Same schema as the configuration file; call screener_config_template for a starter document."
                    },
                    "job_id": text_property(),
                    "workspace": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Local output directory passed as --output-dir; ignored when the remote API executes the launch."
                    }
                }
            }),
            "screener",
            move |arguments| handler.run_screener(arguments),
        ));
    }
    if capabilities.backtest {
        let handler = tools.clone();
        candidates.push(
            Tool::new(
                TOOL_BACKTEST_CONFIG_TEMPLATE,
                "Return a canonical starter backtest configuration document (YAML). \
             Adapt job_id, the time window, symbols, initial_cash, and the \
             strategy parameters, then pass the result as the inline `config` \
             argument of run_backtest when no configuration file exists.",
                json!({
                    "type": "object",
                    "additionalProperties": false,
                    "properties": {}
                }),
                "backtest",
                move |_arguments| handler.backtest_config_template(),
            )
            .read_only(),
        );
    }
    if capabilities.screener {
        let handler = tools.clone();
        candidates.push(
            Tool::new(
                TOOL_SCREENER_CONFIG_TEMPLATE,
                "Return a canonical starter screening configuration document (YAML, \
             `shell: screen`) with an inline example program using \
             `export_screening_program!`. Adapt name, as_of, lookback_days, and \
             rank_metric, then recompute bundle_hash/source_sha256 for the edited source \
             and pass the result as the inline `config` argument of run_screener when no \
             configuration file exists.",
                json!({
                    "type": "object",
                    "additionalProperties": false,
                    "properties": {}
                }),
                "screener",
                move |_arguments| handler.screener_config_template(),
            )
            .read_only(),
        );
    }
    if capabilities.backtest {
        let handler = tools.clone();
        let input_schema = json!({
            "type": "object", "additionalProperties": false, "required": ["model"],
            "properties": {"model": authored_model_schema()}
        });
        candidates.push(Tool::new(
            TOOL_VALIDATE_STRATEGY,
            "Validate an authored Rust strategy without replaying data: compile, load, construct, \
             and check effective subscriptions (including indicators) against harnessConfiguration. \
             Pass {model: {source, harnessConfiguration}} as for validate_study; timestamps require \
             RFC 3339 with Z or an explicit offset; declare exact tagged \
             subscriptions directly or under data (interval-only configuration is \
             rejected). Implement MarketConsumer with type Context = \
             OrderSink, fn subscriptions(&self) -> Vec<Subscription>, and market callbacks on \
             MarketConsumer; implement StrategyProgram::new(parameters: \
             harness_strategy_sdk::serde_json::Value) -> Self. Export at module scope with \
             harness_strategy_sdk::export_strategy_program!(MyStrategy). Bar callbacks receive \
             &BarFrame with bars_by_symbol and missing_symbols. Subscriptions and indicator topology \
             must remain stable. This is not a backtest and does not prove trading performance. \
             Fix compact structured compiler/configuration errors and call validate_strategy again; \
             report infrastructure failures separately. A strategy run uses shell backtest and backtest.json.",
            input_schema,
            "backtest",
            move |arguments| handler.validate_strategy(arguments),
        ));
    }
    if capabilities.job_status {
        let handler = tools.clone();
        candidates.push(Tool::new(
            TOOL_JOB_STATUS,
            "Read one job's persisted state \
             {job_id, kind, state, created_at, updated_at, error_message} from the remote harness API.",
            json!({
                "type": "object",
                "additionalProperties": false,
                "required": ["job_id"],
                "properties": {"job_id": text_property()}
            }),
            "job_status",
            move |arguments| handler.get_job_status(arguments),
        ).read_only());
    }
    if capabilities.artifacts {
        let result_handler = tools.clone();
        candidates.push(
            Tool::new(
                TOOL_GET_JOB_RESULT,
                "Read one stored job's result record \
             {job_id, kind, state, strategy_id, metrics, artifact_count, ...} \
             from the remote harness API.",
                json!({
                    "type": "object",
                    "additionalProperties": false,
                    "required": ["job_id"],
                    "properties": {"job_id": text_property()}
                }),
                "artifacts",
                move |arguments| result_handler.get_job_result(arguments),
            )
            .read_only(),
        );
        let list_handler = tools.clone();
        candidates.push(
            Tool::new(
                TOOL_LIST_ARTIFACTS,
                "List one stored job's artifact manifest entries \
             {name, uri, sha256, sizeBytes} from the remote harness API.",
                json!({
                    "type": "object",
                    "additionalProperties": false,
                    "required": ["job_id"],
                    "properties": {"job_id": text_property()}
                }),
                "artifacts",
                move |arguments| list_handler.list_artifacts(arguments),
            )
            .read_only(),
        );
        let read_handler = tools.clone();
        candidates.push(Tool::new(
            TOOL_READ_ARTIFACT,
            "Read one stored job artifact's contents as utf-8 text or hex-encoded bytes \
             {job_id, name, encoding, content}.",
            json!({
                "type": "object",
                "additionalProperties": false,
                "required": ["job_id", "name"],
                "properties": {
                    "job_id": {"type": "string", "minLength": 1},
                    "name": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Artifact name relative to the job, e.g. metrics.json or artifacts/equity.parquet."
                    }
                }
            }),
            "artifacts",
            move |arguments| read_handler.read_artifact(arguments),
        ).read_only());
    }
    if capabilities.trader {
        let handler = tools.clone();
        candidates.push(Tool::new(
            TOOL_LAUNCH_TRADER,
            "Run one supervised paper trading session through the local 'harness trader' \
             process; the exit code is the session outcome.",
            json!({
                "type": "object",
                "additionalProperties": false,
                "required": ["config_path"],
                "properties": {
                    "config_path": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Path to the trader configuration document."
                    }
                }
            }),
            "trader",
            move |arguments| handler.launch_trader(arguments),
        ));
    }
    ToolCatalog::new(candidates)
}
