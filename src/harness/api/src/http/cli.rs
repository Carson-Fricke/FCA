//! The `api` process command: serve the harness HTTP boundary with axum.
//!
//! Mirrors `harness.cli.api`: the `--config` document is required and must
//! declare the `api` shell, exactly one worker is supported per process, and
//! the bearer-token environment reference is resolved before serving.
//!
//! The shared configuration capability owns parsing, validation and secret syntax.

use std::fmt;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::process::ExitCode;

use clap::Parser;

use crate::http::create_app;
use crate::http::server;

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
    /// Validated bearer-token environment reference.
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

/// Load the shared strict API schema before projecting process settings.
pub fn load_api_settings(path: &std::path::Path) -> Result<ApiSettings, ApiConfigError> {
    let configuration =
        config::load_configuration(path).map_err(|error| ApiConfigError(error.to_string()))?;
    let config::HarnessConfig::Api(document) = configuration else {
        return Err(ApiConfigError(format!(
            "configuration shell must be 'api', not '{}'",
            configuration.shell()
        )));
    };
    let server = &document["server"];
    Ok(ApiSettings {
        shell: "api".into(),
        host: server["host"].as_str().expect("validated host").into(),
        port: server["port"].as_u64().expect("validated port") as u16,
        workers: server["workers"].as_u64().expect("validated workers") as u32,
        bearer_reference: document["auth"]["bearer_token"]
            .as_str()
            .map(str::to_string),
    })
}

/// Resolve only a strict environment reference; literal credentials are rejected.
pub fn resolve_environment_reference(reference: &str) -> Result<String, ApiConfigError> {
    config::resolve_environment_reference(reference, None)
        .map_err(|error| ApiConfigError(error.to_string()))
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
    let mut environ: std::collections::BTreeMap<String, String> = std::env::vars().collect();
    if !environ.contains_key("LAKE_API__BEARER_TOKEN") {
        if let Some(token) = bearer
            .as_ref()
            .or_else(|| environ.get("HARNESS_API_BEARER_TOKEN"))
        {
            environ.insert("LAKE_API__BEARER_TOKEN".into(), token.clone());
        }
    }
    let host = if settings.host == "0.0.0.0" {
        "127.0.0.1"
    } else {
        &settings.host
    };
    let market = lake_hosted::api::create_host(
        &environ,
        std::sync::Arc::new(|| {
            lake_contracts::DateTime::from_micros(
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .expect("system clock")
                    .as_micros()
                    .min(i64::MAX as u128) as i64,
            )
        }),
        &format!("http://{host}:{}/market-data", settings.port),
    )
    .map_err(|e| ApiConfigError(e.to_string()))?;
    let mut config = crate::execution::composed().map_err(|e| ApiConfigError(e.to_string()))?;
    config.bearer_token = bearer;
    let _execution_control = crate::execution::start_control_reconciler();
    let app = create_app(config)
        .map_err(|e| ApiConfigError(e.to_string()))?
        .nest("/market-data", market.router);
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
