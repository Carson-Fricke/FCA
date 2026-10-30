//! Engine identity, reproducibility manifest, and canonical run artifacts.
//!
//! Port of `harness.runtime.backtest.manifest`. The manifest hash uses the same
//! domain-separated SHA-256 over canonical JSON as
//! `harness.configuration.hashing.canonical_hash`; the code fingerprint hashes
//! this crate's deterministic engine sources with the same length-framed,
//! newline-normalized scheme as the Python module fingerprint.

use crate::results::recorder::manifest::digest_bytes;
use harness_contracts::canonical_json::JsonValue;
use harness_contracts::sha256::Sha256;

use super::fixture::DataPreflight;

use super::config::BacktestConfig;

/// Manifest format version.
pub const MANIFEST_VERSION: &str = "1";
/// Stable engine identifier.
pub const ENGINE_ID: &str = "harness.backtest";
/// Stable engine version.
pub const ENGINE_VERSION: &str = "1";

/// Domain prefix used by `harness.configuration.hashing` job result digests.
pub const JOB_DIGEST_DOMAIN: &[u8] = b"fca:harness-results:job:v1\0";
/// Job result digest version tag.
pub const JOB_DIGEST_VERSION: &str = "harness.results.job.v1";

/// Return a domain-separated SHA-256 hash of canonical configuration.
#[must_use]
pub fn configuration_hash(value: &JsonValue) -> String {
    harness_contracts::configuration_hash(
        &value.to_serde_value(),
        harness_contracts::HashEncoding::HistoricalAscii,
    )
}

/// Return the SHA-256 hex digest of one artifact payload.
#[must_use]
pub fn artifact_digest(payload: &[u8]) -> String {
    digest_bytes(payload)
}

/// Generate one bounded, path-safe opaque job identifier.
#[must_use]
pub fn generate_job_id() -> String {
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::time::{SystemTime, UNIX_EPOCH};
    static SEQUENCE: AtomicU64 = AtomicU64::new(0);
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or(0);
    let sequence = SEQUENCE.fetch_add(1, Ordering::Relaxed);
    let mut hasher = Sha256::new();
    hasher.update(&nanos.to_le_bytes());
    hasher.update(&sequence.to_le_bytes());
    hasher.update(&std::process::id().to_le_bytes());
    let digest = hasher.finalize_hex();
    format!("job-{}", &digest[..32])
}

/// Return the domain-separated digest over one job's artifact digests.
#[must_use]
pub fn run_digest(job_id: &str, digests: &std::collections::BTreeMap<String, String>) -> String {
    let artifacts: JsonValue = JsonValue::Object(
        digests
            .iter()
            .map(|(name, digest)| (name.clone(), JsonValue::String(digest.clone())))
            .collect(),
    );
    let payload = JsonValue::object([
        ("domain", JsonValue::string(JOB_DIGEST_VERSION)),
        ("job_id", JsonValue::String(job_id.to_string())),
        ("artifacts", artifacts),
    ]);
    let mut hasher = Sha256::new();
    hasher.update(JOB_DIGEST_DOMAIN);
    hasher.update(payload.to_canonical_string().as_bytes());
    hasher.finalize_hex()
}

/// Return a stable fingerprint of the deterministic backtest engine code.
///
/// Mirrors `code_fingerprint`: newline-normalized source bytes framed by the
/// module name and an 8-byte big-endian length.
#[must_use]
pub fn code_fingerprint() -> String {
    const MODULES: &[(&str, &str)] = &[
        ("backtest.config", include_str!("config.rs")),
        ("backtest.engine_core", include_str!("engine_core.rs")),
        ("backtest.event_bus", include_str!("event_bus.rs")),
        ("backtest.execution", include_str!("execution.rs")),
        ("backtest.fees", include_str!("fees.rs")),
        ("backtest.fixture", include_str!("fixture.rs")),
        ("backtest.history", include_str!("history.rs")),
        ("backtest.identity", include_str!("identity.rs")),
        ("backtest.manifest", include_str!("manifest.rs")),
        ("backtest.mod", include_str!("mod.rs")),
        ("backtest.null", include_str!("null.rs")),
        ("backtest.outcome", include_str!("outcome.rs")),
        ("backtest.portfolio", include_str!("portfolio.rs")),
        ("backtest.preflight", include_str!("preflight.rs")),
        ("backtest.projection", include_str!("projection.rs")),
        ("backtest.risk", include_str!("risk.rs")),
        ("backtest.rng", include_str!("rng.rs")),
        ("backtest.screen", include_str!("screen.rs")),
        ("backtest.stream", include_str!("stream.rs")),
        ("backtest.subscriptions", include_str!("subscriptions.rs")),
        ("backtest.valuation", include_str!("valuation.rs")),
        ("backtest.venue", include_str!("venue.rs")),
        ("backtest.yaml", include_str!("yaml.rs")),
        (
            "trading.accounting",
            include_str!("../../../trading/src/accounting.rs"),
        ),
        (
            "trading.error",
            include_str!("../../../trading/src/error.rs"),
        ),
        (
            "trading.events",
            include_str!("../../../trading/src/events.rs"),
        ),
        (
            "trading.ledger",
            include_str!("../../../trading/src/ledger.rs"),
        ),
        ("trading.lib", include_str!("../../../trading/src/lib.rs")),
        (
            "trading.matching",
            include_str!("../../../trading/src/matching.rs"),
        ),
        (
            "trading.orders",
            include_str!("../../../trading/src/orders.rs"),
        ),
        (
            "trading.program",
            include_str!("../../../trading/src/program.rs"),
        ),
        (
            "trading.replay_errors",
            include_str!("../../../trading/src/replay_errors.rs"),
        ),
        (
            "trading.replay_fills",
            include_str!("../../../trading/src/replay_fills.rs"),
        ),
        (
            "trading.symbol",
            include_str!("../../../trading/src/symbol.rs"),
        ),
        (
            "trading.fee_policy",
            include_str!("../../../trading/src/fee_policy.rs"),
        ),
        (
            "trading.fills.decision",
            include_str!("../../../trading/src/fills/decision.rs"),
        ),
        (
            "trading.fills.fees",
            include_str!("../../../trading/src/fills/fees.rs"),
        ),
        (
            "trading.fills.latency_size",
            include_str!("../../../trading/src/fills/latency_size.rs"),
        ),
        (
            "trading.fills.mid_touch",
            include_str!("../../../trading/src/fills/mid_touch.rs"),
        ),
        (
            "trading.fills.mod",
            include_str!("../../../trading/src/fills/mod.rs"),
        ),
        (
            "trading.fills.simple_touch",
            include_str!("../../../trading/src/fills/simple_touch.rs"),
        ),
        (
            "trading.fills.trades",
            include_str!("../../../trading/src/fills/trades.rs"),
        ),
        (
            "trading.fills.trade_through",
            include_str!("../../../trading/src/fills/trade_through.rs"),
        ),
        (
            "trading.notional_policy",
            include_str!("../../../trading/src/notional_policy.rs"),
        ),
        (
            "trading.oms.groups",
            include_str!("../../../trading/src/oms/groups.rs"),
        ),
        (
            "trading.oms.manager",
            include_str!("../../../trading/src/oms/manager.rs"),
        ),
        (
            "trading.oms.mod",
            include_str!("../../../trading/src/oms/mod.rs"),
        ),
        (
            "trading.oms.state",
            include_str!("../../../trading/src/oms/state.rs"),
        ),
        (
            "trading.portfolio.book",
            include_str!("../../../trading/src/portfolio/book.rs"),
        ),
        (
            "trading.portfolio.hub",
            include_str!("../../../trading/src/portfolio/hub.rs"),
        ),
        (
            "trading.portfolio.mod",
            include_str!("../../../trading/src/portfolio/mod.rs"),
        ),
        (
            "trading.portfolio.options_lifecycle",
            include_str!("../../../trading/src/portfolio/options_lifecycle.rs"),
        ),
        (
            "trading.risk.collateral",
            include_str!("../../../trading/src/risk/collateral.rs"),
        ),
        (
            "trading.risk.kill_switch",
            include_str!("../../../trading/src/risk/kill_switch.rs"),
        ),
        (
            "trading.risk.limits",
            include_str!("../../../trading/src/risk/limits.rs"),
        ),
        (
            "trading.risk.mod",
            include_str!("../../../trading/src/risk/mod.rs"),
        ),
        (
            "trading.risk.models",
            include_str!("../../../trading/src/risk/models.rs"),
        ),
        (
            "trading.risk.pipeline",
            include_str!("../../../trading/src/risk/pipeline.rs"),
        ),
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
    /// Job identifier.
    pub job_id: String,
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
    pub replay_row_fingerprint: Option<String>,
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
    /// Effective consumer plan.
    pub effective_subscriptions: Vec<harness_trading::program::MarketSubscription>,
    /// Consumer union plus execution inputs planned for acquisition.
    pub acquired_subscriptions: Vec<harness_trading::program::MarketSubscription>,
    /// Separate quote/trade execution plan; empty for studies.
    pub execution_subscriptions: Vec<harness_trading::program::MarketSubscription>,
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
        let mut value = JsonValue::object([
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
            ("job_id", JsonValue::String(self.job_id.clone())),
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
                self.replay_row_fingerprint
                    .clone()
                    .map_or(JsonValue::Null, JsonValue::String),
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
        ]);
        if let JsonValue::Object(entries) = &mut value {
            for (name, plan) in [
                ("effective_subscriptions", &self.effective_subscriptions),
                ("acquired_subscriptions", &self.acquired_subscriptions),
                ("execution_subscriptions", &self.execution_subscriptions),
            ] {
                entries.insert(name.into(), super::config::subscriptions_json(plan));
            }
        }
        value
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
/// Returns [`harness_trading::replay_errors::BacktestError`] if the fixture cannot be
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
) -> Result<BacktestManifest, harness_trading::replay_errors::BacktestError> {
    Ok(BacktestManifest {
        manifest_version: MANIFEST_VERSION.to_string(),
        engine_id: ENGINE_ID.to_string(),
        engine_version: ENGINE_VERSION.to_string(),
        code_fingerprint: code_fingerprint(),
        job_id: configuration.job_id.clone(),
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
        effective_subscriptions: Vec::new(),
        acquired_subscriptions: Vec::new(),
        execution_subscriptions: Vec::new(),
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

/// Preserve exact replay bounds in configuration identity, retaining historical
/// six-digit formatting unless the instant has submicrosecond precision.
#[must_use]
pub fn configuration_timestamp(timestamp_ns: i64) -> String {
    let mut text = utc_timestamp(timestamp_ns, true);
    if timestamp_ns.rem_euclid(1_000) != 0 {
        text.truncate(text.len() - 7);
        text.push_str(&format!("{:09}Z", timestamp_ns.rem_euclid(1_000_000_000)));
    }
    text
}

/// Canonical JSON for the fields of a configuration that feed its hash.
#[must_use]
pub fn configuration_to_json(configuration: &BacktestConfig) -> JsonValue {
    JsonValue::object([
        ("job_id", JsonValue::String(configuration.job_id.clone())),
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
