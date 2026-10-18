//! Atomic crypto pair-day ingestion.
//!
//! Ports `lake.ingestion.crypto.transaction`. One transaction covers exactly
//! one UTC calendar day: the session bounds must be midnight to midnight UTC,
//! a partial day is fetched only through the injected clock's current instant
//! (inclusive, like Alpaca's `end`). Closed days publish only `[open, close)`
//! so a midnight print belongs exclusively to the following day. Staged objects
//! plus their coverage rows publish through one injected commit boundary.

use std::cmp::Ordering;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use arrow_array::RecordBatch;
use futures_util::future::{join_all, BoxFuture};
use lake_catalog::{CoverageRecord, CoverageStatus};
use lake_contracts::{symbol_filename, AssetClass, DataError, DataType, Date, DateTime};
use lake_coverage::CoverageSession;
use lake_providers::{NormalizedCryptoQuote, NormalizedCryptoTrade, ProviderError};
use lake_storage::{ParquetMetadata, StorageCapGuard};

use crate::common::{
    failures_all_transient, fetch_error_message, fetch_with_transient_retry, normalize_data_types,
    AtomicIngestCommitter, PartitionWriter, StagedParquetObject, TransientRetry,
};
use crate::providers::CryptoDataProvider;

/// The injected UTC clock used for partial-day fetch bounds.
pub type CryptoClock = Arc<dyn Fn() -> DateTime + Send + Sync>;

/// Published metadata for one crypto pair-day transaction.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CryptoIngestResult {
    /// Canonical uppercase pair.
    pub symbol: String,
    /// UTC day the transaction published.
    pub session_date: Date,
    /// One coverage row per selected data type, in canonical order.
    pub coverage: Vec<CoverageRecord>,
    /// One object metadata entry per selected data type, in canonical order.
    pub objects: Vec<ParquetMetadata>,
    /// Whether the day's fetch failure was a transport-level hiccup: the
    /// backfill may retry it in-ensure; permanent failures are never retried.
    pub retryable: bool,
}

/// Fetch, stage, and atomically publish one crypto pair-day.
#[derive(Clone)]
pub struct CryptoIngestTransaction {
    provider: Arc<dyn CryptoDataProvider>,
    writer: Arc<dyn PartitionWriter>,
    committer: Arc<dyn AtomicIngestCommitter>,
    cap_guard: Option<Arc<StorageCapGuard>>,
    retry: TransientRetry,
    clock: CryptoClock,
}

/// Staged outputs of one transaction, before commit.
struct StagedCryptoObjects {
    objects: Vec<StagedParquetObject>,
    metadata: Vec<ParquetMetadata>,
}

impl CryptoIngestTransaction {
    /// Build a transaction over injected provider, writer, and committer.
    pub fn new(
        provider: Arc<dyn CryptoDataProvider>,
        writer: Arc<dyn PartitionWriter>,
        committer: Arc<dyn AtomicIngestCommitter>,
    ) -> Self {
        Self {
            provider,
            writer,
            committer,
            cap_guard: None,
            retry: TransientRetry::default(),
            clock: Arc::new(system_clock),
        }
    }

    /// Attach a storage capacity guard evaluated before fetch and commit.
    pub fn with_cap_guard(mut self, guard: Arc<StorageCapGuard>) -> Self {
        self.cap_guard = Some(guard);
        self
    }

    /// Override the transient retry budget.
    pub fn with_retry(mut self, retry: TransientRetry) -> Self {
        self.retry = retry;
        self
    }

    /// Override the UTC clock used for partial-day fetch bounds.
    pub fn with_clock(mut self, clock: CryptoClock) -> Self {
        self.clock = clock;
        self
    }

    /// Fetch and publish the requested data types for one UTC day.
    ///
    /// A partial session is queried only through the injected current time.
    /// All fetches finish before any object is staged, and a fetch or
    /// normalization error publishes only error coverage through the same
    /// committer boundary.
    pub async fn run(
        &self,
        pair: &str,
        session: &CoverageSession,
        data_types: Option<&[DataType]>,
    ) -> Result<CryptoIngestResult, DataError> {
        let session_memory = lake_pipeline::Pipeline::global().session().await?;
        let pair = canonical_pair(pair)?;
        let selected = normalize_data_types(data_types, true)?;
        validate_session(session)?;
        // Alpaca's `end` parameter is inclusive, so the fetch window ends at
        // query_end itself. Publication excludes the next UTC day's opening;
        // a partial fetch ending before midnight still includes query_end.
        let query_end = if session.is_partial {
            self.now().min(session.close_time)
        } else {
            session.close_time
        };
        if query_end < session.open_time {
            return Err(DataError::value(
                "partial crypto session clock is before its opening",
            ));
        }
        if let Some(guard) = &self.cap_guard {
            guard.enforce_ingest(0)?;
        }
        let fetches: Vec<BoxFuture<'_, Result<ParquetMetadata, ProviderError>>> = selected
            .iter()
            .map(|data_type| {
                let data_type = *data_type;
                let pair = &pair;
                Box::pin(async move {
                    fetch_with_transient_retry(self.retry, || {
                        Box::pin(self.stage_dataset(data_type, pair, session, query_end))
                    })
                    .await
                }) as BoxFuture<'_, Result<ParquetMetadata, ProviderError>>
            })
            .collect();
        let results = join_all(fetches).await;

        if let Some(message) = fetch_error_message(&selected, &results) {
            let retryable = failures_all_transient(&results);
            let transaction = self.clone();
            let session = *session;
            return crate::common::blocking_stage(move || {
                let _session_memory = session_memory;
                transaction.publish_error(&pair, &session, &selected, message, retryable)
            })
            .await;
        }
        let metadata: Vec<ParquetMetadata> = results
            .into_iter()
            .map(|result| result.expect("a successful gather has no provider error"))
            .collect();

        let transaction = self.clone();
        let session = *session;
        crate::common::blocking_stage(move || {
            let _session_memory = session_memory;
            transaction.finish(pair, &session, selected, metadata)
        })
        .await
    }

    /// Stage and publish on a bounded blocking thread, leaving fetch I/O free.
    fn finish(
        &self,
        pair: String,
        session: &CoverageSession,
        selected: Vec<DataType>,
        metadata: Vec<ParquetMetadata>,
    ) -> Result<CryptoIngestResult, DataError> {
        let staged = StagedCryptoObjects {
            objects: self.writer.drain_staged(),
            metadata,
        };
        if let Some(guard) = &self.cap_guard {
            let additional_bytes = staged
                .objects
                .iter()
                .map(|object| object.metadata.bytes)
                .sum::<u64>();
            if let Err(error) = guard.enforce_ingest(additional_bytes) {
                self.writer.reset_staged();
                return Err(error);
            }
        }

        let coverage = match coverage_records(&pair, session, &selected, &staged.metadata) {
            Ok(coverage) => coverage,
            Err(error) => {
                self.writer.reset_staged();
                return Err(error);
            }
        };
        let outcome = self.committer.commit(&staged.objects, &coverage);
        self.writer.reset_staged();
        outcome?;
        Ok(CryptoIngestResult {
            symbol: pair,
            session_date: session.session_date,
            coverage,
            objects: staged.metadata,
            retryable: false,
        })
    }

    /// Return the injected UTC clock value used for partial-day bounds.
    fn now(&self) -> DateTime {
        (self.clock)()
    }

    async fn stage_dataset(
        &self,
        data: DataType,
        pair: &str,
        session: &CoverageSession,
        query_end: DateTime,
    ) -> Result<ParquetMetadata, ProviderError> {
        let symbols = [pair.to_string()];
        let spec = crate::acquisition::StagingSpec {
            asset: AssetClass::Crypto,
            data,
            day: session.session_date,
            symbol: pair.into(),
        };
        let name = pair.to_string();
        let day = *session;
        match data {
            DataType::Trade => {
                crate::acquisition::stage_pages(
                    self.provider
                        .fetch_crypto_trades(&symbols, session.open_time, query_end),
                    move |rows| trade_rows(&rows, &name, &day, query_end),
                    self.writer.clone(),
                    spec,
                )
                .await
            }
            DataType::Quote => {
                crate::acquisition::stage_pages(
                    self.provider
                        .fetch_crypto_quotes(&symbols, session.open_time, query_end),
                    move |rows| quote_rows(&rows, &name, &day, query_end),
                    self.writer.clone(),
                    spec,
                )
                .await
            }
            _ => Err(ProviderError::Configuration(
                "unsupported tick dataset".into(),
            )),
        }
    }

    /// Publish error coverage for every selected data type.
    fn publish_error(
        &self,
        pair: &str,
        session: &CoverageSession,
        selected: &[DataType],
        message: String,
        retryable: bool,
    ) -> Result<CryptoIngestResult, DataError> {
        self.writer.reset_staged();
        let coverage = selected
            .iter()
            .map(|data_type| {
                CoverageRecord::new(
                    AssetClass::Crypto,
                    *data_type,
                    pair,
                    session.session_date,
                    CoverageStatus::Error,
                    None,
                    0,
                    0,
                    None,
                    Some(message.clone()),
                    None,
                )
            })
            .collect::<Result<Vec<_>, _>>()?;
        self.committer.commit(&[], &coverage)?;
        Ok(CryptoIngestResult {
            symbol: pair.to_string(),
            session_date: session.session_date,
            coverage,
            objects: Vec::new(),
            retryable,
        })
    }
}

/// Build the coverage rows for one staged transaction.
///
/// Counts reflect the published rows after UTC day membership filtering.
fn coverage_records(
    pair: &str,
    session: &CoverageSession,
    selected: &[DataType],
    metadata: &[ParquetMetadata],
) -> Result<Vec<CoverageRecord>, DataError> {
    selected
        .iter()
        .zip(metadata)
        .map(|(data_type, metadata)| {
            let row_count = metadata.rows;
            CoverageRecord::new(
                AssetClass::Crypto,
                *data_type,
                pair,
                session.session_date,
                coverage_status(session.is_partial, row_count),
                Some(metadata.path.clone()),
                row_count,
                metadata.bytes,
                Some(metadata.checksum.clone()),
                None,
                None,
            )
        })
        .collect()
}

/// Return the coverage status for one data type.
fn coverage_status(session_partial: bool, rows: u64) -> CoverageStatus {
    if session_partial {
        CoverageStatus::Partial
    } else if rows > 0 {
        CoverageStatus::Complete
    } else {
        CoverageStatus::Empty
    }
}

/// Validate and canonically order crypto trades.
fn trade_rows(
    records: &[NormalizedCryptoTrade],
    pair: &str,
    session: &CoverageSession,
    query_end: DateTime,
) -> Result<RecordBatch, DataError> {
    let mut validated = Vec::with_capacity(records.len());
    for record in records {
        validate_trade(record, pair, session, query_end)?;
        if record.timestamp < session.close_time {
            validated.push(record);
        }
    }
    validated.sort_by(|left, right| compare_trades(left, right));
    crate::columnar::crypto_trades(&validated, pair)
}

/// Validate and canonically order crypto quotes.
fn quote_rows(
    records: &[NormalizedCryptoQuote],
    pair: &str,
    session: &CoverageSession,
    query_end: DateTime,
) -> Result<RecordBatch, DataError> {
    let mut validated = Vec::with_capacity(records.len());
    for record in records {
        validate_quote(record, pair, session, query_end)?;
        if record.timestamp < session.close_time {
            validated.push(record);
        }
    }
    validated.sort_by(|left, right| compare_quotes(left, right));
    crate::columnar::crypto_quotes(&validated, pair)
}

/// Compare two crypto trades on the pinned `(timestamp, trade_id or -1,
/// exchange or "", price, size, taker_side or "")` order.
pub(crate) fn compare_trades(
    left: &NormalizedCryptoTrade,
    right: &NormalizedCryptoTrade,
) -> Ordering {
    left.timestamp
        .cmp(&right.timestamp)
        .then_with(|| {
            left.trade_id
                .unwrap_or(-1)
                .cmp(&right.trade_id.unwrap_or(-1))
        })
        .then_with(|| {
            left.exchange
                .as_deref()
                .unwrap_or("")
                .cmp(right.exchange.as_deref().unwrap_or(""))
        })
        .then_with(|| left.price.cmp(&right.price))
        .then_with(|| left.size.cmp(&right.size))
        .then_with(|| {
            left.taker_side
                .as_deref()
                .unwrap_or("")
                .cmp(right.taker_side.as_deref().unwrap_or(""))
        })
}

/// Compare two crypto quotes on the pinned `(timestamp, bid_exchange or "",
/// bid_price, bid_size, ask_exchange or "", ask_price, ask_size)` order.
fn compare_quotes(left: &NormalizedCryptoQuote, right: &NormalizedCryptoQuote) -> Ordering {
    left.timestamp
        .cmp(&right.timestamp)
        .then_with(|| {
            left.bid_exchange
                .as_deref()
                .unwrap_or("")
                .cmp(right.bid_exchange.as_deref().unwrap_or(""))
        })
        .then_with(|| left.bid_price.cmp(&right.bid_price))
        .then_with(|| left.bid_size.cmp(&right.bid_size))
        .then_with(|| {
            left.ask_exchange
                .as_deref()
                .unwrap_or("")
                .cmp(right.ask_exchange.as_deref().unwrap_or(""))
        })
        .then_with(|| left.ask_price.cmp(&right.ask_price))
        .then_with(|| left.ask_size.cmp(&right.ask_size))
}

/// Validate one crypto trade against the requested pair and fetch window.
fn validate_trade(
    record: &NormalizedCryptoTrade,
    pair: &str,
    session: &CoverageSession,
    query_end: DateTime,
) -> Result<(), DataError> {
    validate_record_timestamp(record.timestamp, pair, session, query_end)?;
    if record.symbol != pair && canonical_pair(&record.symbol)? != pair {
        return Err(DataError::value(
            "trade symbol does not match the requested pair",
        ));
    }
    Ok(())
}

/// Validate one crypto quote against the requested pair and fetch window.
fn validate_quote(
    record: &NormalizedCryptoQuote,
    pair: &str,
    session: &CoverageSession,
    query_end: DateTime,
) -> Result<(), DataError> {
    validate_record_timestamp(record.timestamp, pair, session, query_end)?;
    if record.symbol != pair && canonical_pair(&record.symbol)? != pair {
        return Err(DataError::value(
            "quote symbol does not match the requested pair",
        ));
    }
    Ok(())
}

/// Validate one record timestamp against the inclusive fetch window.
///
/// The fetch end bound is inclusive, so a print at exactly `query_end` is a
/// valid response. UTC day membership is applied separately before staging.
fn validate_record_timestamp(
    timestamp: DateTime,
    symbol: &str,
    session: &CoverageSession,
    query_end: DateTime,
) -> Result<(), DataError> {
    if timestamp < session.open_time || timestamp > query_end {
        return Err(DataError::value(format!(
            "{symbol} record timestamp is outside UTC day {}",
            session.session_date
        )));
    }
    Ok(())
}

/// Canonicalise and validate one crypto pair.
fn canonical_pair(pair: &str) -> Result<String, DataError> {
    let canonical = pair.trim().to_uppercase();
    let valid = canonical.split_once('/').is_some_and(|(base, quote)| {
        !base.is_empty()
            && !quote.is_empty()
            && base
                .bytes()
                .all(|byte| byte.is_ascii_uppercase() || byte.is_ascii_digit())
            && quote
                .bytes()
                .all(|byte| byte.is_ascii_uppercase() || byte.is_ascii_digit())
    });
    if !valid {
        return Err(DataError::value(format!("invalid crypto pair: {pair:?}")));
    }
    symbol_filename(&canonical)?;
    Ok(canonical)
}

/// Reject a session that is not exactly one UTC calendar day.
fn validate_session(session: &CoverageSession) -> Result<(), DataError> {
    let expected_open = DateTime::midnight(session.session_date);
    let expected_close = DateTime::midnight(session.session_date.add_days(1));
    if session.open_time != expected_open || session.close_time != expected_close {
        return Err(DataError::value(
            "crypto session must be one UTC calendar day",
        ));
    }
    Ok(())
}

/// The production UTC clock: now in microseconds since the Unix epoch.
fn system_clock() -> DateTime {
    match SystemTime::now().duration_since(UNIX_EPOCH) {
        Ok(elapsed) => {
            DateTime::from_micros(i64::try_from(elapsed.as_micros()).unwrap_or(i64::MAX))
        }
        Err(error) => {
            DateTime::from_micros(-i64::try_from(error.duration().as_micros()).unwrap_or(i64::MAX))
        }
    }
}
