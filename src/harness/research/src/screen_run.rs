//! Configuration-level screening runs over the JIT seam.
//!
//! [`run_screening_configuration`] is the complete screening job path: build
//! one [`ScreenContext`] from the injected [`ScreeningDataSource`] port,
//! JIT-compile and run the authored program through the existing
//! [`LoadedScreening`] seam, rank the rows host-side, and write the frozen
//! `screen.json` artifact. There is no data directory or path parameter: data
//! arrives only through the port.
//!
//! A run never fails silently. Program or data failures still write
//! `screen.json` with `status: "failed"` and an `error` string, and return the
//! failed outcome so the caller records a failed job exactly like a study
//! failure. Only an artifact write failure is a hard [`ScreenRunError`].

use std::collections::BTreeMap;
use std::path::Path;
use std::path::PathBuf;

use backtest::backtest::screen::ScreenConfig;
use harness_contracts::sha256::digest_hex;
use harness_strategy_sdk::ScreenContext;
use harness_strategy_sdk::ScreenRow;

use crate::screen::rank_rows;
use crate::source::ScreeningDataSource;
use crate::source::ScreeningDataSourceError;
use harness_strategy_jit::compile;
use harness_strategy_jit::resolve_toolchain;
use harness_strategy_jit::JitError;
use harness_strategy_jit::LoadedScreening;
use harness_strategy_jit::ManagedWorkspace;
use harness_strategy_jit::ProgramKind;

/// The artifact file name every screening run writes.
pub const SCREEN_ARTIFACT: &str = "screen.json";

/// The artifact schema identifier.
pub const SCREEN_SCHEMA: &str = "fca-harness-screen-v2";

/// The domain separator for the screening job digest.
const SCREEN_JOB_DOMAIN: &[u8] = b"fca:harness-screen-job:v1\0";

/// Terminal states of one screening run.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ScreenStatus {
    /// The program ran and the rows are ranked.
    Succeeded,
    /// The run ended with an error; `screen.json` carries the reason.
    Failed,
}

impl ScreenStatus {
    /// The exact artifact spelling.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::Succeeded => "succeeded",
            Self::Failed => "failed",
        }
    }
}

/// One screening run result.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ScreenOutcome {
    /// The execution job identifier.
    pub job_id: String,
    /// Terminal status.
    pub status: ScreenStatus,
    /// Ranked rows; empty for a failed run.
    pub rows: Vec<ScreenRow>,
    /// Failure reason for a failed run.
    pub error: Option<String>,
}

impl ScreenOutcome {
    /// Whether the run completed successfully.
    #[must_use]
    pub fn succeeded(&self) -> bool {
        self.status == ScreenStatus::Succeeded
    }

    /// The number of ranked rows.
    #[must_use]
    pub fn row_count(&self) -> usize {
        self.rows.len()
    }

    /// The failure reason, when the run failed.
    #[must_use]
    pub fn error(&self) -> Option<&str> {
        self.error.as_deref()
    }
}

/// Run options for one screening configuration.
#[derive(Clone, Debug, Default)]
pub struct ScreenRunOptions {
    /// Root of the managed JIT workspace; the default workspace when absent.
    pub workspace_root: Option<PathBuf>,
}

/// A hard screening run failure: the run could not be recorded at all.
#[derive(Debug)]
pub enum ScreenRunError {
    /// The `screen.json` artifact could not be written.
    Artifact(String),
}

impl std::fmt::Display for ScreenRunError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Artifact(detail) => write!(formatter, "screening artifact failed: {detail}"),
        }
    }
}

impl std::error::Error for ScreenRunError {}

/// Run one validated screening configuration and write its `screen.json`.
///
/// The context carries the as-of date, the canonical universe, each symbol's
/// bars, and each symbol's facts; `indicators` is empty until the harness
/// precomputes them. The authored program is compiled (or fetched from the
/// content-addressed cache) and run once, and the returned rows are ranked by
/// the configuration's `rank_metric`.
///
/// # Errors
///
/// Returns [`ScreenRunError::Artifact`] when the output directory or
/// `screen.json` cannot be written. Data-source and program failures are
/// recorded as a failed artifact and a failed outcome instead.
pub fn run_screening_configuration(
    configuration: &ScreenConfig,
    source: &dyn ScreeningDataSource,
    options: &ScreenRunOptions,
    output_dir: Option<&Path>,
) -> Result<(ScreenOutcome, PathBuf), ScreenRunError> {
    let job_id = configuration.job_id.clone();
    let directory = output_dir.map_or_else(
        || PathBuf::from(".local").join("screens").join(&job_id),
        Path::to_path_buf,
    );
    std::fs::create_dir_all(&directory)
        .map_err(|error| ScreenRunError::Artifact(error.to_string()))?;
    let artifact_path = directory.join(SCREEN_ARTIFACT);
    let source_digest = configuration.source_digest();
    let mut toolchain_digest = String::new();
    let mut rows = Vec::new();
    let mut failure: Option<String> = None;

    let opened = match &options.workspace_root {
        Some(root) => ManagedWorkspace::open(root),
        None => ManagedWorkspace::open_default(),
    };
    match opened {
        Err(error) => failure = Some(error.to_string()),
        Ok(workspace) => {
            toolchain_digest = resolve_toolchain(workspace.root())
                .map_or_else(|_| String::new(), |toolchain| toolchain.digest);
            match screen_context(configuration, source) {
                Err(error) => failure = Some(error.to_string()),
                Ok(context) => match screen_rows(&workspace, configuration, &context) {
                    Ok(ranked) => rows = ranked,
                    Err(error) => failure = Some(error.to_string()),
                },
            }
        }
    }

    let status = if failure.is_some() {
        ScreenStatus::Failed
    } else {
        ScreenStatus::Succeeded
    };
    let error = failure;
    let run_digest = run_digest(
        &job_id,
        &source_digest,
        &toolchain_digest,
        configuration,
        status,
    );
    let document = artifact_document(
        configuration,
        &job_id,
        status,
        error.as_deref(),
        &rows,
        &source_digest,
        &toolchain_digest,
        &run_digest,
    )?;
    let mut payload = serde_json::to_string(&document)
        .map_err(|serialize| ScreenRunError::Artifact(serialize.to_string()))?
        .into_bytes();
    payload.push(b'\n');
    std::fs::write(&artifact_path, payload)
        .map_err(|error| ScreenRunError::Artifact(error.to_string()))?;

    Ok((
        ScreenOutcome {
            job_id,
            status,
            rows,
            error,
        },
        artifact_path,
    ))
}

/// Build the screening context from the data-source port.
///
/// The universe is sorted and deduplicated so identical inputs produce
/// byte-identical artifacts regardless of source iteration order.
fn screen_context(
    configuration: &ScreenConfig,
    source: &dyn ScreeningDataSource,
) -> Result<ScreenContext, ScreeningDataSourceError> {
    let as_of = configuration.as_of.clone();
    let mut universe = source.universe(&as_of)?;
    universe.sort();
    universe.dedup();
    let source_bars = source.bars(&universe, "1min", &as_of, configuration.lookback_days)?;
    let mut bars = BTreeMap::new();
    for interval in &configuration.bar_intervals {
        let mut series = BTreeMap::new();
        for symbol in &universe {
            series.insert(
                symbol.clone(),
                crate::source::aggregate_screen_bars(
                    symbol,
                    interval,
                    &as_of,
                    source_bars.get(symbol).cloned().unwrap_or_default(),
                )?,
            );
        }
        bars.insert(interval.clone(), series);
    }
    let facts = source.facts(&universe, &as_of)?;
    Ok(ScreenContext {
        as_of,
        universe,
        bars,
        facts,
        indicators: BTreeMap::new(),
    })
}

/// Compile, load, and run the authored screening program, then rank its rows.
fn screen_rows(
    workspace: &ManagedWorkspace,
    configuration: &ScreenConfig,
    context: &ScreenContext,
) -> Result<Vec<ScreenRow>, JitError> {
    let artifact = compile(workspace, &configuration.source, ProgramKind::Screening)?;
    // Safety: compile produced a harness-strategy-sdk screening cdylib; the
    // loader validates its ABI version and vtable before constructing state.
    let mut loaded =
        unsafe { LoadedScreening::load(&artifact.artifact_path, &configuration.parameters)? };
    let rows = loaded.screen(context)?;
    Ok(rank_rows(rows, &configuration.rank_metric))
}

/// The screening job digest, domain-separated over the job identity.
///
/// `sha256("fca:harness-screen-job:v1\0" || canonical_json({as_of,
/// definition_id, job_id, rank_metric, source_digest, status,
/// toolchain_digest}))`, with object keys sorted. The digest binds the result
/// to the configuration, the compiled source, and the toolchain that built it.
fn run_digest(
    job_id: &str,
    source_digest: &str,
    toolchain_digest: &str,
    configuration: &ScreenConfig,
    status: ScreenStatus,
) -> String {
    let payload = serde_json::json!({
        "as_of": configuration.as_of,
        "definition_id": configuration.definition_id,
        "job_id": job_id,
        "rank_metric": configuration.rank_metric,
        "source_digest": source_digest,
        "status": status.value(),
        "toolchain_digest": toolchain_digest,
    });
    let text = serde_json::to_string(&payload).unwrap_or_else(|_| String::new());
    let mut bytes = Vec::with_capacity(SCREEN_JOB_DOMAIN.len() + text.len());
    bytes.extend_from_slice(SCREEN_JOB_DOMAIN);
    bytes.extend_from_slice(text.as_bytes());
    digest_hex(&bytes)
}

/// Build the frozen `screen.json` document with its exact key set.
#[allow(clippy::too_many_arguments)]
fn artifact_document(
    configuration: &ScreenConfig,
    job_id: &str,
    status: ScreenStatus,
    error: Option<&str>,
    rows: &[ScreenRow],
    source_digest: &str,
    toolchain_digest: &str,
    run_digest: &str,
) -> Result<serde_json::Value, ScreenRunError> {
    let rows = serde_json::to_value(rows)
        .map_err(|serialize| ScreenRunError::Artifact(serialize.to_string()))?;
    let mut object = serde_json::Map::new();
    object.insert("schema".to_string(), serde_json::json!(SCREEN_SCHEMA));
    object.insert("status".to_string(), serde_json::json!(status.value()));
    object.insert("job_id".to_string(), serde_json::json!(job_id));
    object.insert("as_of".to_string(), serde_json::json!(configuration.as_of));
    object.insert(
        "definition_id".to_string(),
        serde_json::json!(configuration.definition_id),
    );
    object.insert(
        "definition_hash".to_string(),
        serde_json::json!(configuration.definition_hash),
    );
    object.insert(
        "bundle_hash".to_string(),
        serde_json::json!(configuration.bundle_hash),
    );
    object.insert(
        "rank_metric".to_string(),
        serde_json::json!(configuration.rank_metric),
    );
    object.insert(
        "bar_intervals".to_string(),
        serde_json::json!(configuration.bar_intervals),
    );
    object.insert(
        "data_window".to_string(),
        serde_json::json!({
            "start": configuration.window_start(),
            "end": configuration.window_end(),
        }),
    );
    object.insert(
        "universe_revision".to_string(),
        serde_json::json!(configuration.universe_revision),
    );
    object.insert("rows".to_string(), rows);
    if let Some(error) = error {
        object.insert("error".to_string(), serde_json::json!(error));
    }
    object.insert(
        "provenance".to_string(),
        serde_json::json!({
            "source_digest": source_digest,
            "toolchain_digest": toolchain_digest,
            "run_digest": run_digest,
        }),
    );
    Ok(serde_json::Value::Object(object))
}

#[cfg(test)]
#[path = "../tests/unit/screen_run.rs"]
mod tests;
