//! Owns MCP handler composition. Protocol ports, discovery and each operation family have separate owners.
use std::collections::BTreeMap;
use std::sync::Arc;

use percent_encoding::utf8_percent_encode;
use percent_encoding::AsciiSet;
use percent_encoding::NON_ALPHANUMERIC;
use serde_json::json;
use serde_json::Map;
use serde_json::Value;
use sha2::Digest;
use sha2::Sha256;

use crate::auth::BearerTokenSource;

pub mod arguments;
pub mod catalog;
mod launch;
pub mod ports;
mod queries;
mod templates;
mod transport;
mod validation;
use arguments::*;
use ports::*;
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

struct Handlers {
    launch: launch::LaunchTools,
    validation: validation::ValidationTools,
    queries: queries::QueryTools,
}
fn compose(context: HarnessToolsContext) -> Result<Handlers, ContextError> {
    let context = context.validate()?;
    let remote = Arc::new(transport::RemoteClient {
        credentials: context.credentials,
        api_base_url: context.api_base_url,
        remote_transport: context.remote_transport,
    });
    Ok(Handlers {
        launch: launch::LaunchTools {
            mode: context.mode,
            backtest_submitter: context.backtest_submitter,
            study_submitter: context.study_submitter,
            screening_submitter: context.screening_submitter,
            runner: context.runner,
            program: context.program,
            configuration_loader: context.configuration_loader,
            remote: remote.clone(),
        },
        validation: validation::ValidationTools {
            mode: context.mode,
            study_validator: context.study_validator,
            sdk_reference: context.sdk_reference,
            remote: remote.clone(),
        },
        queries: queries::QueryTools {
            mode: context.mode,
            job_store: context.job_store,
            remote,
        },
    })
}
