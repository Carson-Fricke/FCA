//! The `mcp` process command: serve the harness MCP server over stdio.
//!
//! Mirrors `harness.mcp.__main__`: the `--mode` selects the `run_backtest`
//! boundary, `--api-base-url` (or `HARNESS_API_BASE_URL`) configures the
//! remote query boundary, and `--enable-trader` publishes the capability-gated
//! `launch_trader` tool. The local process runner and the remote HTTP
//! transport are composed here, the only place a real subprocess or socket is
//! created.

use std::collections::BTreeMap;
use std::fmt;
use std::process::{Command, ExitCode};
use std::sync::Arc;

use clap::Parser;
use mcp::auth::{EnvironmentBearerTokenSource, BEARER_TOKEN_VARIABLE};
use mcp::server::McpServer;
use mcp::store::{FileSystemJobStore, JobStore};
use mcp::tools::{
    Boundary, ConfigurationLoader, HarnessToolsContext, ProcessOutcome, ProcessRunner,
    RemoteResponse, RemoteTransport,
};
use mcp::transport::serve_stdio;
use serde_json::{Map, Value};

use crate::local::{
    NativeBacktestSubmitter, NativeScreeningSubmitter, NativeStudySubmitter, NativeStudyValidator,
};

/// Environment variable consulted for the remote API base URL.
pub const API_BASE_URL_VARIABLE: &str = "HARNESS_API_BASE_URL";

/// A failure composing or serving the MCP process.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct McpServeError(pub String);

impl fmt::Display for McpServeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl std::error::Error for McpServeError {}

/// Options for the standalone `mcp` entry point.
#[derive(Debug, Clone, Parser)]
#[command(
    name = "mcp",
    about = "Serve the harness MCP server over stdio.",
    disable_help_subcommand = true
)]
pub struct McpCli {
    /// The shared `harness mcp` options.
    #[command(flatten)]
    pub args: crate::McpArgs,
}

/// Local process boundary that spawns the configured harness process.
#[derive(Debug, Clone, Copy, Default)]
pub struct SubprocessRunner;

impl ProcessRunner for SubprocessRunner {
    fn run(&self, argv: &[String]) -> Result<ProcessOutcome, String> {
        let Some((program, arguments)) = argv.split_first() else {
            return Err("empty argv".to_string());
        };
        let output = Command::new(program)
            .args(arguments)
            .output()
            .map_err(|error| error.to_string())?;
        Ok(ProcessOutcome {
            returncode: output.status.code().unwrap_or(-1),
            stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
            stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
        })
    }
}

fn decode_response(response: ureq::Response) -> Result<RemoteResponse, String> {
    let status_code = response.status();
    let mut headers = BTreeMap::new();
    for name in response.headers_names() {
        if let Some(value) = response.header(&name) {
            headers.insert(name, value.to_string());
        }
    }
    let text = response.into_string().unwrap_or_default();
    let body = if text.trim().is_empty() {
        Value::Object(Map::new())
    } else {
        serde_json::from_str(&text).unwrap_or_else(|_| Value::Object(Map::new()))
    };
    Ok(RemoteResponse {
        status_code,
        headers,
        body,
    })
}

/// Remote harness API boundary over HTTP with decoded JSON bodies.
#[derive(Debug, Clone, Copy, Default)]
pub struct HttpRemoteTransport;

impl RemoteTransport for HttpRemoteTransport {
    fn get(&self, url: &str, headers: &BTreeMap<String, String>) -> Result<RemoteResponse, String> {
        let mut request = ureq::get(url);
        for (name, value) in headers {
            request = request.set(name, value);
        }
        match request.call() {
            Ok(response) => decode_response(response),
            Err(ureq::Error::Status(_, response)) => decode_response(response),
            Err(error) => Err(error.to_string()),
        }
    }

    fn post(
        &self,
        url: &str,
        headers: &BTreeMap<String, String>,
        payload: &Value,
    ) -> Result<RemoteResponse, String> {
        let mut request = ureq::post(url);
        for (name, value) in headers {
            request = request.set(name, value);
        }
        match request.send_json(payload.clone()) {
            Ok(response) => decode_response(response),
            Err(ureq::Error::Status(_, response)) => decode_response(response),
            Err(error) => Err(error.to_string()),
        }
    }
}

/// Resolve the remote API base URL from the flag or environment.
#[must_use]
pub fn resolve_base_url(api_base_url: Option<&str>) -> Option<String> {
    let explicit = api_base_url
        .map(str::trim)
        .filter(|value| !value.is_empty());
    match explicit {
        Some(value) => Some(value.to_string()),
        None => std::env::var(API_BASE_URL_VARIABLE)
            .ok()
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty()),
    }
}

/// Compose the MCP server from the documented deployment settings.
///
/// # Errors
///
/// Returns [`McpServeError`] when the mode is unknown or the context fails
/// validation.
pub fn build_mcp_server(args: &crate::McpArgs) -> Result<McpServer, McpServeError> {
    let boundary = Boundary::parse(&args.mode).map_err(|error| McpServeError(error.to_string()))?;
    let base_url = resolve_base_url(args.api_base_url.as_deref());
    let mut context = HarnessToolsContext::new(boundary, Arc::new(EnvironmentBearerTokenSource));
    context.capabilities.trader = args.enable_trader;
    context.program = vec!["harness".to_string()];
    context.api_base_url = base_url.clone();
    context.remote_transport = base_url
        .as_ref()
        .map(|_| Arc::new(HttpRemoteTransport) as Arc<dyn RemoteTransport>);
    context.runner = Some(Arc::new(SubprocessRunner));
    context.configuration_loader = default_loader();
    if boundary == Boundary::Local {
        let root = crate::http::state::results_root_from_env();
        let job_store: Arc<dyn JobStore> = Arc::new(FileSystemJobStore::new(&root));
        context.job_store = Some(job_store.clone());
        context.backtest_submitter =
            Some(Arc::new(NativeBacktestSubmitter::new(job_store.clone())));
        context.study_submitter = Some(Arc::new(NativeStudySubmitter::new(job_store.clone())));
        context.screening_submitter = Some(Arc::new(NativeScreeningSubmitter::new(
            job_store,
            Arc::new(crate::http::native::NativeScreenRunner::default()),
        )));
        context.study_validator = Some(Arc::new(NativeStudyValidator));
    }
    let context = context
        .validate()
        .map_err(|error| McpServeError(error.to_string()))?;
    mcp::create_server(context).map_err(|error| McpServeError(error.to_string()))
}

fn default_loader() -> Arc<dyn ConfigurationLoader> {
    Arc::new(mcp::tools::JsonFileConfigurationLoader)
}

/// Serve the MCP server over stdio until EOF.
///
/// # Errors
///
/// Returns [`McpServeError`] when the server cannot be composed or stdio
/// fails.
pub fn serve_mcp(args: &crate::McpArgs) -> Result<usize, McpServeError> {
    let server = build_mcp_server(args)?;
    let stdin = std::io::stdin();
    let stdout = std::io::stdout();
    let stderr = std::io::stderr();
    let mut out = stdout.lock();
    let mut diagnostics = stderr.lock();
    serve_stdio(&server, stdin.lock(), &mut out, &mut diagnostics)
        .map_err(|error| McpServeError(format!("mcp server failed: {error}")))
}

/// Blocking entry point for the standalone `mcp` binary and `harness mcp`.
#[must_use]
pub fn serve_mcp_blocking(args: &crate::McpArgs) -> ExitCode {
    match serve_mcp(args) {
        Ok(_) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("{error}");
            ExitCode::from(2)
        }
    }
}

/// The bearer variable name surfaced in diagnostics and settings.
#[must_use]
pub fn bearer_variable() -> &'static str {
    BEARER_TOKEN_VARIABLE
}
