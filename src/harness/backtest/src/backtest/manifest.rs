//! Engine identity, reproducibility manifest, and canonical run artifacts.
//!
//! Port of `harness.runtime.backtest.manifest`. The manifest hash uses the same
//! domain-separated SHA-256 over canonical JSON as
//! `harness.configuration.hashing.canonical_hash`; the code fingerprint hashes
//! this crate's deterministic engine sources with the same length-framed,
//! newline-normalized scheme as the Python module fingerprint.

use crate::canonical_json::JsonValue;
use crate::results::recorder::manifest::digest_bytes;
use crate::sha256::{digest_hex, Sha256};

use super::errors::BacktestStatus;
use super::fixture::DataPreflight;

use super::config::BacktestConfig;

/// Manifest format version.
pub const MANIFEST_VERSION: &str = "1";
/// Stable engine identifier.
pub const ENGINE_ID: &str = "harness.backtest";
/// Stable engine version.
pub const ENGINE_VERSION: &str = "1";

/// Domain prefix used by `harness.configuration.hashing`.
const HASH_DOMAIN: &[u8] = b"fca:harness-configuration:v1\0";
/// Domain prefix used by `harness.configuration.hashing` run digests.
pub const RUN_DIGEST_DOMAIN: &[u8] = b"fca:harness-results:run:v1\0";
/// Run digest version tag.
pub const RUN_DIGEST_VERSION: &str = "harness.results.run.v1";

/// Return a domain-separated SHA-256 hash of canonical configuration.
#[must_use]
pub fn configuration_hash(value: &JsonValue) -> String {
    let mut hasher = Sha256::new();
    hasher.update(HASH_DOMAIN);
    hasher.update(value.to_canonical_string().as_bytes());
    hasher.finalize_hex()
}

/// Return the SHA-256 hex digest of one artifact payload.
#[must_use]
pub fn artifact_digest(payload: &[u8]) -> String {
    digest_bytes(payload)
}

/// Return the domain-separated digest over one run's artifact digests.
#[must_use]
pub fn run_digest(run_id: &str, digests: &std::collections::BTreeMap<String, String>) -> String {
    let artifacts: JsonValue = JsonValue::Object(
        digests
            .iter()
            .map(|(name, digest)| (name.clone(), JsonValue::String(digest.clone())))
            .collect(),
    );
    let payload = JsonValue::object([
        ("domain", JsonValue::string(RUN_DIGEST_VERSION)),
        ("run_id", JsonValue::String(run_id.to_string())),
        ("artifacts", artifacts),
    ]);
    let mut hasher = Sha256::new();
    hasher.update(RUN_DIGEST_DOMAIN);
    hasher.update(payload.to_canonical_string().as_bytes());
    hasher.finalize_hex()
}

/// Return a stable fingerprint of the deterministic backtest engine code.
///
/// Mirrors `code_fingerprint`: newline-normalized source bytes framed by the
/// module name and an 8-byte big-endian length.
#[must_use]
pub fn code_fingerprint() -> String {
    const MODULES: [(&str, &str); 12] = [
        (
            "harness-backtest.backtest.config",
            include_str!("config.rs"),
        ),
        (
            "harness-backtest.backtest.engine",
            include_str!("engine.rs"),
        ),
        (
            "harness-backtest.backtest.engine_core",
            include_str!("engine_core.rs"),
        ),
        (
            "harness-backtest.backtest.errors",
            include_str!("errors.rs"),
        ),
        ("harness-backtest.backtest.fees", include_str!("fees.rs")),
        ("harness-backtest.backtest.fills", include_str!("fills.rs")),
        (
            "harness-backtest.backtest.fixture",
            include_str!("fixture.rs"),
        ),
        (
            "harness-backtest.backtest.manifest",
            include_str!("manifest.rs"),
        ),
        (
            "harness-backtest.backtest.portfolio",
            include_str!("portfolio.rs"),
        ),
        (
            "harness-backtest.backtest.preflight",
            include_str!("preflight.rs"),
        ),
        ("harness-backtest.backtest.rng", include_str!("rng.rs")),
        ("harness-backtest.backtest.yaml", include_str!("yaml.rs")),
    ];
    let mut hasher = Sha256::new();
    hasher.update(b"fca-backtest-code-v1\0");
    for (name, source) in MODULES {
        let payload = source.replace("\r\n", "\n");
        let frame = format!("{name}\u{0}{payload}");
        hasher.update(&(frame.len() as u64).to_be_bytes());
        hasher.update(frame.as_bytes());
    }
    hasher.finalize_hex()
}

/// Reproducibility identity for one configured strategy instance.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StrategyIdentity {
    /// Instance identifier.
    pub instance_id: String,
    /// `module:Class` implementation path.
    pub implementation: String,
    /// Content hash of the strategy bundle.
    pub bundle_hash: String,
    /// Parameter hash.
    pub parameter_hash: String,
    /// Symbols the instance trades.
    pub symbols: Vec<String>,
    /// String-exact capital fraction.
    pub capital_fraction: String,
}

impl StrategyIdentity {
    /// Return a canonical JSON-ready representation.
    #[must_use]
    pub fn to_json(&self) -> JsonValue {
        JsonValue::object([
            ("instance_id", JsonValue::String(self.instance_id.clone())),
            (
                "implementation",
                JsonValue::String(self.implementation.clone()),
            ),
            ("bundle_hash", JsonValue::String(self.bundle_hash.clone())),
            (
                "parameter_hash",
                JsonValue::String(self.parameter_hash.clone()),
            ),
            (
                "symbols",
                JsonValue::Array(
                    self.symbols
                        .iter()
                        .map(|symbol| JsonValue::String(symbol.clone()))
                        .collect(),
                ),
            ),
            (
                "capital_fraction",
                JsonValue::String(self.capital_fraction.clone()),
            ),
        ])
    }
}

/// Every reproducibility input for one deterministic backtest run.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BacktestManifest {
    /// Manifest format version.
    pub manifest_version: String,
    /// Engine identifier.
    pub engine_id: String,
    /// Engine version.
    pub engine_version: String,
    /// Code fingerprint.
    pub code_fingerprint: String,
    /// Run identifier.
    pub run_id: String,
    /// Configuration shell.
    pub shell: String,
    /// Schema version.
    pub schema_version: u64,
    /// Configuration hash.
    pub configuration_hash: String,
    /// Run seed.
    pub seed: u64,
    /// Run window start.
    pub start: String,
    /// Run window end.
    pub end: String,
    /// Base currency.
    pub base_currency: String,
    /// String-exact initial cash.
    pub initial_cash: String,
    /// Data snapshot identifier.
    pub data_snapshot_id: String,
    /// Data fingerprint.
    pub data_fingerprint: String,
    /// Data source identifier.
    pub data_source_id: String,
    /// Replay file fingerprint.
    pub replay_file_fingerprint: String,
    /// Replay row fingerprint.
    pub replay_row_fingerprint: String,
    /// Replay data fingerprint.
    pub replay_data_fingerprint: String,
    /// Fill model name.
    pub fill_model: String,
    /// Fill model fingerprint.
    pub fill_model_fingerprint: String,
    /// Fee schedule identifier.
    pub fee_schedule: String,
    /// Fee configuration fingerprint.
    pub fee_fingerprint: String,
    /// Risk pipeline fingerprint.
    pub risk_pipeline_fingerprint: String,
    /// Strategy identities.
    pub strategies: Vec<StrategyIdentity>,
}

impl BacktestManifest {
    /// Return the SHA-256 hash of every reproducibility input.
    #[must_use]
    pub fn manifest_hash(&self) -> String {
        configuration_hash(&self.to_json())
    }

    /// Return a canonical JSON-ready representation.
    #[must_use]
    pub fn to_json(&self) -> JsonValue {
        JsonValue::object([
            (
                "manifest_version",
                JsonValue::String(self.manifest_version.clone()),
            ),
            ("engine_id", JsonValue::String(self.engine_id.clone())),
            (
                "engine_version",
                JsonValue::String(self.engine_version.clone()),
            ),
            (
                "code_fingerprint",
                JsonValue::String(self.code_fingerprint.clone()),
            ),
            ("run_id", JsonValue::String(self.run_id.clone())),
            ("shell", JsonValue::String(self.shell.clone())),
            ("schema_version", JsonValue::UInt(self.schema_version)),
            (
                "configuration_hash",
                JsonValue::String(self.configuration_hash.clone()),
            ),
            ("seed", JsonValue::UInt(self.seed)),
            ("start", JsonValue::String(self.start.clone())),
            ("end", JsonValue::String(self.end.clone())),
            (
                "base_currency",
                JsonValue::String(self.base_currency.clone()),
            ),
            ("initial_cash", JsonValue::String(self.initial_cash.clone())),
            (
                "data_snapshot_id",
                JsonValue::String(self.data_snapshot_id.clone()),
            ),
            (
                "data_fingerprint",
                JsonValue::String(self.data_fingerprint.clone()),
            ),
            (
                "data_source_id",
                JsonValue::String(self.data_source_id.clone()),
            ),
            (
                "replay_file_fingerprint",
                JsonValue::String(self.replay_file_fingerprint.clone()),
            ),
            (
                "replay_row_fingerprint",
                JsonValue::String(self.replay_row_fingerprint.clone()),
            ),
            (
                "replay_data_fingerprint",
                JsonValue::String(self.replay_data_fingerprint.clone()),
            ),
            ("fill_model", JsonValue::String(self.fill_model.clone())),
            (
                "fill_model_fingerprint",
                JsonValue::String(self.fill_model_fingerprint.clone()),
            ),
            ("fee_schedule", JsonValue::String(self.fee_schedule.clone())),
            (
                "fee_fingerprint",
                JsonValue::String(self.fee_fingerprint.clone()),
            ),
            (
                "risk_pipeline_fingerprint",
                JsonValue::String(self.risk_pipeline_fingerprint.clone()),
            ),
            (
                "strategies",
                JsonValue::Array(
                    self.strategies
                        .iter()
                        .map(StrategyIdentity::to_json)
                        .collect(),
                ),
            ),
        ])
    }
}

/// The subset of one deterministic backtest outcome the staged port records.
///
/// The full Python `BacktestResult` also carries fills, fee assessments, the
/// portfolio snapshot, and risk decisions; those domain types arrive with the
/// engine, portfolio, and risk ports, so this result carries the reproducibility
/// manifest, preflight evidence, dispatch identity, and outcome counts.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BacktestResult {
    /// Reproducibility manifest.
    pub manifest: BacktestManifest,
    /// Terminal status.
    pub status: BacktestStatus,
    /// Abort reason, if any.
    pub reason: Option<String>,
    /// Preflight evidence.
    pub preflight: DataPreflight,
    /// Event bus dispatch hash.
    pub dispatch_hash: String,
    /// Events dispatched on the bus.
    pub dispatched_count: u64,
    /// Market events consumed.
    pub market_event_count: u64,
    /// Orders submitted.
    pub order_count: u64,
    /// Fills produced.
    pub fill_count: u64,
    /// Approved orders in dispatch order.
    pub orders: Vec<super::engine::OrderIntent>,
    /// Fills produced in dispatch order.
    pub fills: Vec<super::engine::Fill>,
    /// Risk decisions recorded.
    pub risk_decision_count: u64,
    /// Halted strategies.
    pub halted_strategies: Vec<String>,
    /// Stopped strategies.
    pub stopped_strategies: Vec<String>,
    /// Teardown errors.
    pub teardown_errors: Vec<String>,
    /// Lookahead violations.
    pub lookahead_violations: Vec<String>,
}

impl BacktestResult {
    /// Whether the run replayed its complete fixture.
    #[must_use]
    pub fn completed(&self) -> bool {
        self.status == BacktestStatus::Completed
    }

    /// Return the canonical, JSON-ready run artifacts.
    #[must_use]
    pub fn artifacts(&self) -> JsonValue {
        JsonValue::object([
            ("manifest", self.manifest.to_json()),
            ("status", JsonValue::string(self.status.value())),
            (
                "reason",
                match &self.reason {
                    Some(reason) => JsonValue::String(reason.clone()),
                    None => JsonValue::Null,
                },
            ),
            ("preflight", self.preflight.to_json()),
            (
                "dispatch_hash",
                JsonValue::String(self.dispatch_hash.clone()),
            ),
            (
                "counts",
                JsonValue::object([
                    ("dispatched", JsonValue::UInt(self.dispatched_count)),
                    ("market_events", JsonValue::UInt(self.market_event_count)),
                    ("orders", JsonValue::UInt(self.order_count)),
                    ("fills", JsonValue::UInt(self.fill_count)),
                    ("risk_decisions", JsonValue::UInt(self.risk_decision_count)),
                ]),
            ),
            (
                "strategies",
                JsonValue::object([
                    ("halted", string_array(&self.halted_strategies)),
                    ("stopped", string_array(&self.stopped_strategies)),
                    ("teardown_errors", string_array(&self.teardown_errors)),
                    (
                        "lookahead_violations",
                        string_array(&self.lookahead_violations),
                    ),
                ]),
            ),
        ])
    }

    /// Return the stable byte-for-byte artifact document for this run.
    #[must_use]
    pub fn artifact_json(&self) -> String {
        self.artifacts().to_canonical_string()
    }

    /// Return the SHA-256 digest of the complete run artifact.
    #[must_use]
    pub fn artifact_digest(&self) -> String {
        digest_hex(self.artifact_json().as_bytes())
    }

    /// Return the SHA-256 digest of the run outcome without provenance.
    #[must_use]
    pub fn outcome_digest(&self) -> String {
        let artifacts = self.artifacts();
        let JsonValue::Object(mut entries) = artifacts else {
            unreachable!("artifacts is an object")
        };
        entries.remove("manifest");
        entries.remove("preflight");
        digest_hex(JsonValue::Object(entries).to_canonical_string().as_bytes())
    }
}

fn string_array(values: &[String]) -> JsonValue {
    JsonValue::Array(
        values
            .iter()
            .map(|value| JsonValue::String(value.clone()))
            .collect(),
    )
}

/// Build the reproducibility manifest for one planned backtest run.
///
/// # Errors
///
/// Returns [`super::errors::BacktestError`] if the fixture cannot be
/// preflighted.
#[allow(clippy::too_many_arguments)]
pub fn build_manifest(
    configuration: &BacktestConfig,
    fixture: &super::fixture::ReplayFixture,
    preflight: &DataPreflight,
    fill_model: &str,
    fill_model_identity: &JsonValue,
    fee_schedule: &str,
    fee_identity: &JsonValue,
    risk_pipeline_identity: &JsonValue,
    strategy_identities: Vec<StrategyIdentity>,
) -> Result<BacktestManifest, super::errors::BacktestError> {
    Ok(BacktestManifest {
        manifest_version: MANIFEST_VERSION.to_string(),
        engine_id: ENGINE_ID.to_string(),
        engine_version: ENGINE_VERSION.to_string(),
        code_fingerprint: code_fingerprint(),
        run_id: configuration.run_id.clone(),
        shell: configuration.shell.clone(),
        schema_version: configuration.schema_version,
        configuration_hash: configuration.configuration_hash(),
        seed: configuration.seed,
        start: utc_timestamp(configuration.start_ns, false),
        end: utc_timestamp(configuration.end_ns, false),
        base_currency: configuration.base_currency.clone(),
        initial_cash: configuration.initial_cash.clone(),
        data_snapshot_id: configuration.data_snapshot_id.clone(),
        data_fingerprint: configuration.data_fingerprint.clone(),
        data_source_id: fixture.source_id.clone(),
        replay_file_fingerprint: preflight.fingerprints.file.clone(),
        replay_row_fingerprint: preflight.fingerprints.row.clone(),
        replay_data_fingerprint: preflight.fingerprints.data.clone(),
        fill_model: fill_model.to_string(),
        fill_model_fingerprint: configuration_hash(fill_model_identity),
        fee_schedule: fee_schedule.to_string(),
        fee_fingerprint: configuration_hash(fee_identity),
        risk_pipeline_fingerprint: configuration_hash(risk_pipeline_identity),
        strategies: strategy_identities,
    })
}

/// Format an epoch nanosecond instant as the Python `datetime.isoformat`
/// timespec-microseconds string for a UTC-aware value.
///
/// `with_z` selects the canonical-hash form (`Z` suffix); otherwise the
/// artifact form (`+00:00` suffix) the manifest fields use.
#[must_use]
pub fn utc_timestamp(timestamp_ns: i64, with_z: bool) -> String {
    let mut text = crate::results::recorder::format_timestamp_ns(timestamp_ns);
    if with_z {
        if text.ends_with("+00:00") {
            text.truncate(text.len() - 6);
        }
        if !text.ends_with('Z') {
            text.push('Z');
        }
    } else if text.ends_with('Z') {
        text.pop();
        text.push_str("+00:00");
    }
    text
}

/// Canonical JSON for the fields of a configuration that feed its hash.
#[must_use]
pub fn configuration_to_json(configuration: &BacktestConfig) -> JsonValue {
    JsonValue::object([
        ("run_id", JsonValue::String(configuration.run_id.clone())),
        ("shell", JsonValue::String(configuration.shell.clone())),
        (
            "schema_version",
            JsonValue::UInt(configuration.schema_version),
        ),
        ("seed", JsonValue::UInt(configuration.seed)),
        ("start_ns", JsonValue::Int(configuration.start_ns)),
        ("end_ns", JsonValue::Int(configuration.end_ns)),
        (
            "base_currency",
            JsonValue::String(configuration.base_currency.clone()),
        ),
        (
            "initial_cash",
            JsonValue::String(configuration.initial_cash.clone()),
        ),
        ("symbols", string_array(&configuration.symbols)),
    ])
}
