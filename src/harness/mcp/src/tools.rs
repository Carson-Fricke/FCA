//! Tool catalog and execution boundaries for the harness MCP server.
//!
//! The registry mirrors the Python `harness.mcp.tools` surface exactly:
//! `run_backtest`, `get_backtest_status`, `get_run`, `list_artifacts`,
//! `run_sweep`, and the capability-gated `launch_trader`. Every boundary is
//! injected; the server composes no subprocess or HTTP client itself, and
//! serialized settings never contain a token value.

use std::collections::BTreeMap;
use std::fmt;
use std::sync::Arc;

use percent_encoding::{utf8_percent_encode, AsciiSet, NON_ALPHANUMERIC};
use serde_json::{json, Map, Value};

use crate::auth::{BearerTokenSource, BEARER_TOKEN_VARIABLE};

/// Tool name for the backtest runner.
pub const TOOL_RUN_BACKTEST: &str = "run_backtest";
/// Tool name for job status queries.
pub const TOOL_BACKTEST_STATUS: &str = "get_backtest_status";
/// Tool name for run queries.
pub const TOOL_GET_RUN: &str = "get_run";
/// Tool name for artifact listing.
pub const TOOL_LIST_ARTIFACTS: &str = "list_artifacts";
/// Tool name for the local sweep runner.
pub const TOOL_RUN_SWEEP: &str = "run_sweep";
/// Tool name for the capability-gated trader launch.
pub const TOOL_LAUNCH_TRADER: &str = "launch_trader";

const LAUNCH_ROUTE: &str = "/v1/backtests/jobs";
const JOB_STATUS_ROUTE: &str = "/v1/backtests/jobs";
const RUN_ROUTE: &str = "/v1/backtests/runs";
const IDENTIFIER_MAX: usize = 128;

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
    /// Publish `get_backtest_status`.
    pub backtest_status: bool,
    /// Publish `get_run` and `list_artifacts`.
    pub artifacts: bool,
    /// Publish `run_sweep`.
    pub sweeps: bool,
    /// Publish `launch_trader` (opt-in).
    pub trader: bool,
}

impl Default for ToolCapabilities {
    fn default() -> Self {
        Self {
            backtest: true,
            backtest_status: true,
            artifacts: true,
            sweeps: true,
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
            "backtest_status": self.backtest_status,
            "artifacts": self.artifacts,
            "sweeps": self.sweeps,
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

/// Injected local sweeps surface for one spec document path.
pub trait SweepSubmitter: Send + Sync {
    /// Expand and submit one sweep, returning the submission summary.
    ///
    /// # Errors
    ///
    /// Returns a reason string when the sweep cannot be completed.
    fn submit(&self, spec_path: &str) -> Result<Value, String>;
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
    /// Local sweep submitter.
    pub sweep_submitter: Option<Arc<dyn SweepSubmitter>>,
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
            sweep_submitter: None,
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
                if self.runner.is_none() {
                    return Err(ContextError(
                        "local execution requires a callable ProcessRunner".to_string(),
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
            handler: Arc::new(handler),
        }
    }

    /// The `tools/list` entry for this tool.
    #[must_use]
    pub fn schema_entry(&self) -> Value {
        json!({
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
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
        let config_path = text_argument(arguments, "config_path")?;
        if self.context.mode == Boundary::Remote {
            return self.launch_remote_backtest(&config_path);
        }
        let mut tail = vec![
            "backtest-job".to_string(),
            "--config".to_string(),
            config_path,
        ];
        tail.extend(workspace_arguments(arguments)?);
        self.invoke_process(&tail, "backtest-job")
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
    pub fn get_backtest_status(
        &self,
        arguments: &Map<String, Value>,
    ) -> Result<Value, ToolExecutionError> {
        let job_id = identifier_argument(arguments, "job_id")?;
        let url = self.remote_url(&format!(
            "{JOB_STATUS_ROUTE}/{}",
            utf8_percent_encode(&job_id, PATH_SEGMENT)
        ))?;
        self.remote_get(&url, &format!("backtest job '{job_id}' does not exist"))
    }

    /// Read one stored run's catalog row from the remote harness API.
    ///
    /// # Errors
    ///
    /// Returns [`ToolExecutionError`] when the remote call fails.
    pub fn get_run(&self, arguments: &Map<String, Value>) -> Result<Value, ToolExecutionError> {
        let run_id = identifier_argument(arguments, "run_id")?;
        let url = self.remote_url(&format!(
            "{RUN_ROUTE}/{}",
            utf8_percent_encode(&run_id, PATH_SEGMENT)
        ))?;
        self.remote_get(&url, &format!("backtest run '{run_id}' does not exist"))
    }

    /// List one stored run's artifacts from the remote harness API.
    ///
    /// # Errors
    ///
    /// Returns [`ToolExecutionError`] when the remote call fails.
    pub fn list_artifacts(
        &self,
        arguments: &Map<String, Value>,
    ) -> Result<Value, ToolExecutionError> {
        let run_id = identifier_argument(arguments, "run_id")?;
        let url = self.remote_url(&format!(
            "{RUN_ROUTE}/{}/artifacts",
            utf8_percent_encode(&run_id, PATH_SEGMENT)
        ))?;
        self.remote_get(&url, &format!("backtest run '{run_id}' does not exist"))
    }

    /// Expand and submit one sweep through the injected local runner.
    ///
    /// # Errors
    ///
    /// Returns [`ToolExecutionError`] when no submitter is composed or the
    /// local runner fails.
    pub fn run_sweep(&self, arguments: &Map<String, Value>) -> Result<Value, ToolExecutionError> {
        let spec_path = text_argument(arguments, "spec_path")?;
        let Some(submitter) = &self.context.sweep_submitter else {
            return Err(ToolExecutionError::new(
                "sweep execution is not composed in this MCP server; \
                 sweeps run through the local sweeps runner only",
            ));
        };
        submitter.submit(&spec_path).map_err(|_| {
            ToolExecutionError::new("the local sweep runner could not complete the sweep")
        })
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

    fn launch_remote_backtest(&self, config_path: &str) -> Result<Value, ToolExecutionError> {
        let payload = self
            .context
            .configuration_loader
            .load_json(config_path)
            .map_err(|error| {
                ToolExecutionError::new(format!(
                    "could not load the backtest configuration '{config_path}': {error}"
                ))
            })?;
        let url = self.remote_url(LAUNCH_ROUTE)?;
        let response = self.dispatch_remote_post(&url, &payload)?;
        let status_code = response.status_code;
        if status_code == 401 || status_code == 403 {
            return Err(credential_rejected(status_code));
        }
        if status_code == 404 || status_code == 501 {
            return Err(ToolExecutionError::new(format!(
                "the remote harness API does not implement backtest launch (HTTP {status_code})"
            )));
        }
        if !(200..300).contains(&status_code) {
            return Err(ToolExecutionError::new(format!(
                "remote backtest launch failed (HTTP {status_code})"
            )));
        }
        let job_id = response
            .body
            .get("job_id")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| {
                ToolExecutionError::new(
                    "remote harness launch response requires a non-empty 'job_id' string",
                )
            })?;
        let state = response
            .body
            .get("state")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| {
                ToolExecutionError::new(
                    "remote harness launch response requires a non-empty 'state' string",
                )
            })?;
        let location = response
            .headers
            .iter()
            .find(|(name, _)| name.eq_ignore_ascii_case("location"))
            .map(|(_, value)| value.clone());
        Ok(json!({
            "boundary": "remote",
            "job_id": job_id,
            "state": state,
            "location": location,
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
        transport
            .post(url, &headers, payload)
            .map_err(|_| ToolExecutionError::new("the remote harness API could not be reached"))
    }

    fn remote_headers(&self) -> Result<BTreeMap<String, String>, ToolExecutionError> {
        self.context.credentials.bearer_headers().map_err(|error| {
            ToolExecutionError::new(format!(
                "remote harness API calls need a bearer credential: {error}"
            ))
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
}

fn credential_rejected(status_code: u16) -> ToolExecutionError {
    ToolExecutionError::new(format!(
        "the remote harness API rejected the bearer credential (HTTP {status_code})"
    ))
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

/// Build the capability-filtered tool catalog for one composition.
///
/// # Errors
///
/// Returns [`ContextError`] when the catalog cannot be composed.
pub fn build_catalog(context: HarnessToolsContext) -> Result<ToolCatalog, ContextError> {
    let capabilities = context.capabilities;
    let tools = Arc::new(HarnessTools::new(context));
    let mut candidates: Vec<Tool> = Vec::new();

    if capabilities.backtest {
        let handler = tools.clone();
        candidates.push(Tool::new(
            TOOL_RUN_BACKTEST,
            "Run one deterministic backtest: a local 'harness backtest-job' process, \
             or a bearer-authenticated POST to the remote harness API returning {job_id, state}.",
            json!({
                "type": "object",
                "additionalProperties": false,
                "required": ["config_path"],
                "properties": {
                    "config_path": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Path to the backtest configuration document."
                    },
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
    if capabilities.backtest_status {
        let handler = tools.clone();
        candidates.push(Tool::new(
            TOOL_BACKTEST_STATUS,
            "Read one backtest job's persisted state \
             {job_id, state, created_at, updated_at, error_message} from the remote harness API.",
            json!({
                "type": "object",
                "additionalProperties": false,
                "required": ["job_id"],
                "properties": {"job_id": text_property()}
            }),
            "backtest_status",
            move |arguments| handler.get_backtest_status(arguments),
        ));
    }
    if capabilities.artifacts {
        let get_run_handler = tools.clone();
        candidates.push(Tool::new(
            TOOL_GET_RUN,
            "Read one stored run's catalog row {run_id, status, strategy_id, metrics, ...} \
             from the remote harness API.",
            json!({
                "type": "object",
                "additionalProperties": false,
                "required": ["run_id"],
                "properties": {"run_id": text_property()}
            }),
            "artifacts",
            move |arguments| get_run_handler.get_run(arguments),
        ));
        let list_handler = tools.clone();
        candidates.push(Tool::new(
            TOOL_LIST_ARTIFACTS,
            "List one stored run's artifact summaries {name, size, object_path, sas_url} \
             from the remote harness API.",
            json!({
                "type": "object",
                "additionalProperties": false,
                "required": ["run_id"],
                "properties": {"run_id": text_property()}
            }),
            "artifacts",
            move |arguments| list_handler.list_artifacts(arguments),
        ));
    }
    if capabilities.sweeps {
        let handler = tools.clone();
        candidates.push(Tool::new(
            TOOL_RUN_SWEEP,
            "Expand and submit one parameter sweep through the local sweeps runner; \
             returns {sweep_id, requested, expanded, duplicates_skipped, run_ids}.",
            json!({
                "type": "object",
                "additionalProperties": false,
                "required": ["spec_path"],
                "properties": {
                    "spec_path": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Path to the JSON sweep specification document."
                    }
                }
            }),
            "sweeps",
            move |arguments| handler.run_sweep(arguments),
        ));
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::auth::{bearer_headers, EnvironmentBearerTokenSource};

    struct EchoRunner;

    impl ProcessRunner for EchoRunner {
        fn run(&self, argv: &[String]) -> Result<ProcessOutcome, String> {
            Ok(ProcessOutcome {
                returncode: 0,
                stdout: argv.join(" "),
                stderr: String::new(),
            })
        }
    }

    fn local_context() -> HarnessToolsContext {
        let mut context =
            HarnessToolsContext::new(Boundary::Local, Arc::new(EnvironmentBearerTokenSource));
        context.runner = Some(Arc::new(EchoRunner));
        context.validate().expect("valid context")
    }

    #[test]
    fn default_catalog_is_capability_filtered() {
        let catalog = build_catalog(local_context()).expect("catalog");
        assert_eq!(
            catalog.names(),
            vec![
                "run_backtest",
                "get_backtest_status",
                "get_run",
                "list_artifacts",
                "run_sweep"
            ]
        );
        let mut opted_in = local_context();
        opted_in.capabilities.trader = true;
        let catalog = build_catalog(opted_in).expect("catalog");
        assert!(catalog.names().contains(&"launch_trader"));
    }

    #[test]
    fn run_backtest_invokes_the_local_runner() {
        let catalog = build_catalog(local_context()).expect("catalog");
        let tool = catalog.resolve(TOOL_RUN_BACKTEST).expect("tool");
        let mut arguments = Map::new();
        arguments.insert("config_path".to_string(), json!("backtest.yaml"));
        arguments.insert("workspace".to_string(), json!("out"));
        let payload = tool.call(&arguments).expect("call");
        assert_eq!(payload["boundary"], "local");
        assert_eq!(payload["exit_code"], 0);
        assert_eq!(
            payload["stdout"],
            "harness backtest-job --config backtest.yaml --output-dir out"
        );
    }

    #[test]
    fn settings_never_contain_a_token() {
        let context = local_context();
        let settings = context.as_settings();
        assert_eq!(settings["bearer_env_var"], BEARER_TOKEN_VARIABLE);
        assert!(!settings.to_string().contains("Bearer "));
        let _ = bearer_headers(Some("x"));
    }

    #[test]
    fn remote_boundary_requires_https_and_transport() {
        let mut context =
            HarnessToolsContext::new(Boundary::Remote, Arc::new(EnvironmentBearerTokenSource));
        context.api_base_url = Some("http://example.test".to_string());
        assert!(context.validate().is_err());
    }
}
