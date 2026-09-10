//! The `api` process command: serve the harness HTTP boundary with axum.
//!
//! Mirrors `harness.cli.api`: the `--config` document is required and must
//! declare the `api` shell, exactly one worker is supported per process, and
//! the bearer-token environment reference is resolved before serving.
//!
//! Configuration loading here is deliberately minimal and line-oriented; the
//! behavior-identical typed loader lands with the configuration port task, at
//! which point this resolves through that package instead.

use std::fmt;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::process::ExitCode;

use clap::Parser;

use crate::http::state::AppConfig;
use crate::http::{create_app, server};

/// The `api` configuration subsets the process boundary needs.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ApiSettings {
    /// The declared configuration shell (must be `api`).
    pub shell: String,
    /// Bind host.
    pub host: String,
    /// Bind port.
    pub port: u16,
    /// Worker count (must be `1`).
    pub workers: u32,
    /// Raw bearer-token value or environment reference.
    pub bearer_reference: Option<String>,
}

/// A configuration problem surfaced before serving.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ApiConfigError(pub String);

impl fmt::Display for ApiConfigError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl std::error::Error for ApiConfigError {}

/// Options for the standalone `api` entry point.
#[derive(Debug, Clone, Parser)]
#[command(
    name = "api",
    about = "Serve quant-harness results and job operations.",
    disable_help_subcommand = true
)]
pub struct ApiCli {
    /// The shared `harness api` options.
    #[command(flatten)]
    pub args: crate::ApiArgs,
}

fn scalar(value: &str) -> String {
    value
        .trim()
        .trim_matches(|character| character == '"' || character == '\'')
        .to_string()
}

/// Parse the fields this boundary needs from an API configuration document.
///
/// # Errors
///
/// Returns [`ApiConfigError`] when the file cannot be read.
pub fn load_api_settings(path: &std::path::Path) -> Result<ApiSettings, ApiConfigError> {
    let text = std::fs::read_to_string(path)
        .map_err(|error| ApiConfigError(format!("configuration could not be loaded: {error}")))?;
    let mut settings = ApiSettings {
        shell: String::new(),
        host: "127.0.0.1".to_string(),
        port: 8001,
        workers: 1,
        bearer_reference: None,
    };
    let mut section = String::new();
    for line in text.lines() {
        let line = match line.split_once('#') {
            Some((code, _)) => code,
            None => line,
        };
        if line.trim().is_empty() {
            continue;
        }
        let indent = line.len() - line.trim_start().len();
        let trimmed = line.trim();
        let Some((key, raw_value)) = trimmed.split_once(':') else {
            continue;
        };
        let key = key.trim();
        let value = scalar(raw_value);
        if indent == 0 {
            section = key.to_string();
            if key == "shell" {
                settings.shell = value;
            }
        } else {
            match (section.as_str(), key) {
                ("server", "host") => settings.host = value,
                ("server", "port") => {
                    settings.port = value.parse().map_err(|_| {
                        ApiConfigError("server.port must be an integer".to_string())
                    })?;
                }
                ("server", "workers") => {
                    settings.workers = value.parse().map_err(|_| {
                        ApiConfigError("server.workers must be an integer".to_string())
                    })?;
                }
                ("auth", "bearer_token") => settings.bearer_reference = Some(value),
                _ => {}
            }
        }
    }
    Ok(settings)
}

/// Resolve a `${VAR}` environment reference to its value.
///
/// # Errors
///
/// Returns [`ApiConfigError`] naming the unset variable.
pub fn resolve_environment_reference(reference: &str) -> Result<String, ApiConfigError> {
    let trimmed = reference.trim();
    if let Some(inner) = trimmed
        .strip_prefix("${")
        .and_then(|value| value.strip_suffix('}'))
    {
        return std::env::var(inner)
            .map_err(|_| ApiConfigError(format!("environment variable '{inner}' is not set")));
    }
    Ok(trimmed.to_string())
}

/// Resolve the settings, host/port overrides, and bearer token for serving.
///
/// # Errors
///
/// Returns [`ApiConfigError`] for the Python CLI's usage failures.
pub fn resolve_api_settings(
    args: &crate::ApiArgs,
) -> Result<(ApiSettings, Option<String>), ApiConfigError> {
    let Some(path) = args.config.clone() else {
        return Err(ApiConfigError(
            "An API configuration is required: api --config <path>".to_string(),
        ));
    };
    let mut settings = load_api_settings(&path)?;
    if settings.shell != "api" {
        return Err(ApiConfigError(format!(
            "configuration shell must be 'api', not '{}'",
            settings.shell
        )));
    }
    if settings.workers != 1 {
        return Err(ApiConfigError(
            "the api process serves one worker per process; scale by starting additional processes"
                .to_string(),
        ));
    }
    if let Some(host) = &args.host {
        settings.host.clone_from(host);
    }
    if let Some(port) = args.port {
        settings.port = port;
    }
    let bearer = match &settings.bearer_reference {
        Some(reference) => Some(resolve_environment_reference(reference)?),
        None => None,
    };
    Ok((settings, bearer))
}

/// Serve the API until the process is stopped.
///
/// # Errors
///
/// Returns [`ApiConfigError`] for configuration or bind failures.
pub async fn serve_api(args: &crate::ApiArgs) -> Result<(), ApiConfigError> {
    let (settings, bearer) = resolve_api_settings(args)?;
    let mut config = AppConfig::in_memory();
    config.bearer_token = bearer;
    let app = create_app(config).map_err(|error| ApiConfigError(error.to_string()))?;
    let addr: SocketAddr = format!("{}:{}", settings.host, settings.port)
        .parse()
        .map_err(|error| ApiConfigError(format!("invalid bind address: {error}")))?;
    server::serve(app, addr)
        .await
        .map_err(|error| ApiConfigError(format!("api server failed: {error}")))
}

/// Blocking entry point for the standalone `api` binary and `harness api`.
#[must_use]
pub fn serve_api_blocking(args: &crate::ApiArgs) -> ExitCode {
    let runtime = match tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
    {
        Ok(runtime) => runtime,
        Err(error) => {
            eprintln!("api server could not start: {error}");
            return ExitCode::from(2);
        }
    };
    match runtime.block_on(serve_api(args)) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("{error}");
            ExitCode::from(2)
        }
    }
}

/// Path helper retained for callers that need the resolved config path.
#[must_use]
pub fn config_path(args: &crate::ApiArgs) -> Option<PathBuf> {
    args.config.clone()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_config(name: &str, body: &str) -> PathBuf {
        let path = std::env::temp_dir().join(format!(
            "harness-api-cli-{}-{name}.yaml",
            std::process::id()
        ));
        std::fs::write(&path, body).expect("write");
        path
    }

    #[test]
    fn loads_server_and_auth_fields() {
        let path = temp_config(
            "api",
            "shell: api\nserver:\n  host: 0.0.0.0\n  port: 9000\n  workers: 1\nauth:\n  bearer_token: \"${HARNESS_API_BEARER_TOKEN}\"\n",
        );
        let settings = load_api_settings(&path).expect("load");
        assert_eq!(settings.shell, "api");
        assert_eq!(settings.host, "0.0.0.0");
        assert_eq!(settings.port, 9000);
        assert_eq!(settings.workers, 1);
        assert_eq!(
            settings.bearer_reference.as_deref(),
            Some("${HARNESS_API_BEARER_TOKEN}")
        );
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn missing_config_is_a_usage_error() {
        let error = resolve_api_settings(&crate::ApiArgs::default()).expect_err("error");
        assert!(error.0.contains("--config"));
    }

    #[test]
    fn non_api_shell_is_rejected() {
        let path = temp_config("backtest", "shell: backtest\n");
        let args = crate::ApiArgs {
            config: Some(path.clone()),
            host: None,
            port: None,
        };
        let error = resolve_api_settings(&args).expect_err("error");
        assert!(error.0.contains("shell must be 'api'"));
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn environment_reference_resolution_names_the_variable() {
        let error =
            resolve_environment_reference("${FCA_DEFINITELY_UNSET_VARIABLE}").expect_err("error");
        assert!(error.0.contains("FCA_DEFINITELY_UNSET_VARIABLE"));
        assert!(resolve_environment_reference("literal").expect("literal") == "literal");
    }

    #[test]
    fn api_args_parse_from_cli() {
        let cli = ApiCli::try_parse_from([
            "api",
            "--config",
            "config/harness/api.yaml",
            "--host",
            "0.0.0.0",
            "--port",
            "9001",
        ])
        .expect("parse");
        assert_eq!(cli.args.host.as_deref(), Some("0.0.0.0"));
        assert_eq!(cli.args.port, Some(9001));
    }
}
