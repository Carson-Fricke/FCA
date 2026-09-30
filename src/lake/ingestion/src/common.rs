//! Shared ports, data-type normalization, and retry helpers for atomic ingest.
//!
//! Ports `lake.ingestion.common`: the staged-object and committer boundary the
//! equity and crypto transactions publish through, the canonical data-type
//! selection rule (`None` selects both trade and quote in publication order),
//! and the transient-failure retry budget. Only transport-level provider
//! failures ([`ProviderError::Transport`] and [`ProviderError::Timeout`]) are
//! retried; every other failure propagates on its first occurrence.

use std::time::Duration;

use futures_util::future::BoxFuture;
use lake_catalog::CoverageRecord;
use lake_contracts::{symbol_filename, AssetClass, DataError, DataType, Date};
use lake_providers::ProviderError;
use lake_storage::ParquetMetadata;
use serde_json::Value;

/// An immutable, fully written Parquet payload waiting for one atomic commit.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StagedParquetObject {
    /// Metadata for the exact bytes carried by `payload`.
    pub metadata: ParquetMetadata,
    /// The complete Parquet object payload.
    pub payload: Vec<u8>,
}

/// Whole-partition replacement with a private staging area.
///
/// `write` serializes one dataset into the writer's private staging area;
/// `drain_staged` returns every object written since the last drain, and
/// `reset_staged` discards anything not yet drained. Staged bytes never become
/// externally visible before the committer publishes them.
pub trait PartitionWriter: Send + Sync {
    /// Serialize one dataset and stage its object.
    ///
    /// `interval` is the bars interval partition dimension; bars datasets pass
    /// their canonical token and every other data type passes `None`.
    fn write(
        &self,
        records: &[Value],
        session_date: Date,
        asset_class: AssetClass,
        data_type: DataType,
        interval: Option<&str>,
        symbol: &str,
    ) -> Result<ParquetMetadata, DataError>;

    /// Take every staged object, leaving the writer empty.
    fn drain_staged(&self) -> Vec<StagedParquetObject>;

    /// Discard every staged object without publishing it.
    fn reset_staged(&self);
}

/// Publish objects and coverage rows as one atomic unit.
pub trait AtomicIngestCommitter: Send + Sync {
    /// Commit every staged object and coverage row, or none of them.
    fn commit(
        &self,
        objects: &[StagedParquetObject],
        coverage: &[CoverageRecord],
    ) -> Result<(), DataError>;
}

/// Attempts and backoff for transient fetch retries within one session.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct TransientRetry {
    /// Total fetch attempts, including the first.
    pub attempts: u32,
    /// Seconds multiplied by the attempt number between retries.
    pub base_delay_seconds: f64,
}

impl Default for TransientRetry {
    fn default() -> Self {
        Self {
            attempts: 3,
            base_delay_seconds: 1.0,
        }
    }
}

/// The ingest data types, in canonical publication order.
const CANONICAL_DATA_TYPES: [DataType; 2] = [DataType::Trade, DataType::Quote];

/// Return the requested data types in canonical trade-then-quote order.
///
/// `None` selects both data types so a caller that does not care keeps the
/// whole-session behaviour. An empty selection or one containing anything but
/// trade and quote is rejected with the Python message for the asset class.
pub(crate) fn normalize_data_types(
    values: Option<&[DataType]>,
    crypto: bool,
) -> Result<Vec<DataType>, DataError> {
    let message = if crypto {
        "crypto data_types must contain trades or quotes"
    } else {
        "data_types must contain trade or quote"
    };
    let Some(values) = values else {
        return Ok(CANONICAL_DATA_TYPES.to_vec());
    };
    if values.is_empty()
        || values
            .iter()
            .any(|value| !CANONICAL_DATA_TYPES.contains(value))
    {
        return Err(DataError::value(message));
    }
    Ok(CANONICAL_DATA_TYPES
        .into_iter()
        .filter(|data_type| values.contains(data_type))
        .collect())
}

/// Run one fetch, retrying transient provider failures with growing backoff.
///
/// This is the transient/permanent discrimination point: only
/// [`ProviderError::is_transient`] failures are retried, up to
/// `retry.attempts` total calls; every other failure propagates on the first
/// occurrence. A zero-attempt budget still performs one call rather than
/// panicking.
pub(crate) async fn fetch_with_transient_retry<'a, T, F>(
    retry: TransientRetry,
    mut call: F,
) -> Result<T, ProviderError>
where
    F: FnMut() -> BoxFuture<'a, Result<T, ProviderError>>,
{
    let budget = retry.attempts.max(1);
    let mut attempt = 1;
    loop {
        match call().await {
            Ok(value) => return Ok(value),
            Err(error) => {
                if attempt >= budget || !error.is_transient() {
                    return Err(error);
                }
                let delay = retry.base_delay_seconds * f64::from(attempt);
                if delay > 0.0 {
                    tokio::time::sleep(Duration::from_secs_f64(delay)).await;
                }
                attempt += 1;
            }
        }
    }
}

/// Format one provider failure the way Python's `_error_message` does.
///
/// The Python exception class name becomes the variant's lake-level name, and
/// an empty detail renders as the name alone.
pub(crate) fn provider_error_message(error: &ProviderError) -> String {
    let kind = match error {
        ProviderError::Http { .. } => "AlpacaHTTPError",
        ProviderError::Transport(_) => "TransportError",
        ProviderError::Timeout(_) => "TimeoutError",
        ProviderError::Protocol(_) => "AlpacaProtocolError",
        ProviderError::Configuration(_) => "ValueError",
    };
    detail_message(kind, &error.to_string())
}

/// Format one data-layer failure the way Python's `_error_message` does.
pub(crate) fn data_error_message(error: &DataError) -> String {
    let kind = match error {
        DataError::TypeError(_) => "TypeError",
        DataError::ValueError(_) => "ValueError",
        DataError::Lookahead(_) => "LookaheadError",
        DataError::Incomplete(_) => "IncompleteError",
        DataError::Unsupported(_) => "UnsupportedError",
        DataError::EnsureJob(_) => "EnsureJobError",
        DataError::Arithmetic(_) => "ArithmeticError",
        DataError::Protocol(_) => "ProtocolError",
        DataError::NotFound(_) => "NotFoundError",
        DataError::Conflict(_) => "ConflictError",
        DataError::Io(_) => "IoError",
    };
    detail_message(kind, &error.to_string())
}

fn detail_message(kind: &str, detail: &str) -> String {
    let detail = detail.trim();
    if detail.is_empty() {
        kind.to_string()
    } else {
        format!("{kind}: {detail}")
    }
}

/// Join one `"{data_type}: {failure}"` message per failed type.
///
/// `None` means every fetch succeeded. Results align with `data_types` by
/// position, exactly as `asyncio.gather(return_exceptions=True)` preserves
/// argument order.
pub(crate) fn fetch_error_message<T>(
    data_types: &[DataType],
    results: &[Result<T, ProviderError>],
) -> Option<String> {
    let mut errors = Vec::new();
    for (data_type, result) in data_types.iter().zip(results) {
        if let Err(error) = result {
            errors.push(format!(
                "{}: {}",
                data_type.value(),
                provider_error_message(error)
            ));
        }
    }
    (!errors.is_empty()).then(|| errors.join("; "))
}

/// Return whether every failing fetch failed transiently.
///
/// A session whose fetches failed only at the transport level (DNS,
/// connection, timeout) may be retried in-ensure; a session with a permanent
/// failure re-runs into the same wall and must not be.
pub(crate) fn failures_all_transient<T>(results: &[Result<T, ProviderError>]) -> bool {
    let mut failures = 0;
    for error in results.iter().filter_map(|result| result.as_ref().err()) {
        failures += 1;
        if !error.is_transient() {
            return false;
        }
    }
    failures > 0
}

/// Canonicalise and validate one market symbol.
pub(crate) fn canonical_symbol(symbol: &str) -> Result<String, DataError> {
    let canonical = symbol.trim().to_uppercase();
    symbol_filename(&canonical)?;
    Ok(canonical)
}
