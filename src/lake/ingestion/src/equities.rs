//! Atomic, dependency-injected ingestion for one equity symbol session.
//!
//! Ports `lake.ingestion.equities.transaction`. The transaction fetches every
//! requested data type concurrently, validates and deterministically sorts the
//! provider records, stages one Parquet object per data type through the
//! injected [`PartitionWriter`], and publishes objects plus coverage rows
//! through one [`AtomicIngestCommitter`] call. A fetch or validation failure
//! publishes error coverage through the same boundary and never a partial
//! success.

use std::cmp::Ordering;
use std::sync::Arc;

use arrow_array::RecordBatch;
use futures_util::future::{join_all, BoxFuture};
use lake_catalog::{CoverageRecord, CoverageStatus};
use lake_contracts::{AssetClass, DataError, DataType, Date};
use lake_coverage::CoverageSession;
use lake_providers::{NormalizedQuote, NormalizedTrade, ProviderError};
use lake_storage::{ParquetMetadata, StorageCapGuard};

use crate::common::{
    canonical_symbol, data_error_message, failures_all_transient, fetch_error_message,
    fetch_with_transient_retry, normalize_data_types, ordered_records, AtomicIngestCommitter,
    PartitionWriter, TransientRetry,
};
use crate::providers::EquityDataProvider;

/// Published metadata for one symbol and session transaction.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EquityIngestResult {
    /// Canonical uppercase symbol.
    pub symbol: String,
    /// Session date the transaction published.
    pub session_date: Date,
    /// One coverage row per selected data type, in canonical order.
    pub coverage: Vec<CoverageRecord>,
    /// One object metadata entry per selected data type, in canonical order.
    pub objects: Vec<ParquetMetadata>,
    /// Whether the session's fetch failure was a transport-level hiccup: the
    /// backfill may retry it in-ensure; permanent failures are never retried.
    pub retryable: bool,
}

/// Fetch, stage, and atomically publish one equity symbol session.
#[derive(Clone)]
pub struct EquityIngestTransaction {
    provider: Arc<dyn EquityDataProvider>,
    writer: Arc<dyn PartitionWriter>,
    committer: Arc<dyn AtomicIngestCommitter>,
    cap_guard: Option<Arc<StorageCapGuard>>,
    retry: TransientRetry,
}

/// Fetched records for one selected data type.
enum EquityRecords {
    Trades(Vec<NormalizedTrade>),
    Quotes(Vec<NormalizedQuote>),
}

/// Staged outputs of one transaction, before commit.
struct StagedEquityObjects {
    objects: Vec<crate::common::StagedParquetObject>,
    metadata: Vec<ParquetMetadata>,
}

impl EquityIngestTransaction {
    /// Build a transaction over injected provider, writer, and committer.
    pub fn new(
        provider: Arc<dyn EquityDataProvider>,
        writer: Arc<dyn PartitionWriter>,
        committer: Arc<dyn AtomicIngestCommitter>,
    ) -> Self {
        Self {
            provider,
            writer,
            committer,
            cap_guard: None,
            retry: TransientRetry::default(),
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

    /// Fetch and publish the requested data types for one planned session.
    ///
    /// Every provider request completes before staging begins. Fetch or
    /// normalization failures publish only error metadata through the same
    /// atomic boundary; a failed commit is propagated without a compensating
    /// write that could hide a partial backend failure.
    ///
    /// `data_types` selects the published datasets. `None` selects both trades
    /// and quotes in canonical order, so a caller that does not care keeps the
    /// whole-session behaviour.
    pub async fn run(
        &self,
        symbol: &str,
        session: &CoverageSession,
        data_types: Option<&[DataType]>,
    ) -> Result<EquityIngestResult, DataError> {
        let session_memory = lake_pipeline::Pipeline::global().session().await?;
        let symbol = canonical_symbol(symbol)?;
        let selected = normalize_data_types(data_types, false)?;

        if let Some(guard) = &self.cap_guard {
            guard.enforce_ingest(0)?;
        }
        // Alpaca's `end` parameter is inclusive, so the fetch window is
        // [open_time, close_time] on both ends: a raw print stamped exactly at
        // the session close (the 16:00:00 ET closing auction print) is part of
        // this session's data and must be accepted by record validation.
        let fetches: Vec<BoxFuture<'_, Result<EquityRecords, ProviderError>>> = selected
            .iter()
            .map(|data_type| {
                let data_type = *data_type;
                let provider = self.provider.as_ref();
                let symbol = symbol.as_str();
                let retry = self.retry;
                Box::pin(
                    async move { fetch_records(provider, data_type, symbol, session, retry).await },
                ) as BoxFuture<'_, Result<EquityRecords, ProviderError>>
            })
            .collect();
        let results = join_all(fetches).await;

        if let Some(message) = fetch_error_message(&selected, &results) {
            let retryable = failures_all_transient(&results);
            let transaction = self.clone();
            let session = *session;
            return crate::common::blocking_stage(move || {
                let _session_memory = session_memory;
                transaction.publish_error(&symbol, &session, &selected, message, retryable)
            })
            .await;
        }
        let records: Vec<EquityRecords> = results
            .into_iter()
            .map(|result| result.expect("a successful gather has no provider error"))
            .collect();

        let transaction = self.clone();
        let session = *session;
        crate::common::blocking_stage(move || {
            let _session_memory = session_memory;
            transaction.finish(symbol, &session, selected, records)
        })
        .await
    }

    /// Stage and publish on a bounded blocking thread, leaving fetch I/O free.
    fn finish(
        &self,
        symbol: String,
        session: &CoverageSession,
        selected: Vec<DataType>,
        records: Vec<EquityRecords>,
    ) -> Result<EquityIngestResult, DataError> {
        let staged_result = self.stage(&symbol, session, &selected, &records);
        let staged = match staged_result {
            Ok(staged) => staged,
            Err(error) => {
                self.writer.reset_staged();
                return self.publish_error(
                    &symbol,
                    session,
                    &selected,
                    data_error_message(&error),
                    false,
                );
            }
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

        let coverage = match coverage_records(&symbol, session, &selected, &staged.metadata) {
            Ok(coverage) => coverage,
            Err(error) => {
                self.writer.reset_staged();
                return Err(error);
            }
        };
        let outcome = self.committer.commit(&staged.objects, &coverage);
        self.writer.reset_staged();
        outcome?;
        Ok(EquityIngestResult {
            symbol,
            session_date: session.session_date,
            coverage,
            objects: staged.metadata,
            retryable: false,
        })
    }

    /// Validate, sort, and stage every selected dataset.
    fn stage(
        &self,
        symbol: &str,
        session: &CoverageSession,
        selected: &[DataType],
        records: &[EquityRecords],
    ) -> Result<StagedEquityObjects, DataError> {
        let pipeline = lake_pipeline::Pipeline::global();
        let _cpu = pipeline.cpu_blocking()?;
        let mut metadata = Vec::with_capacity(selected.len());
        for (data_type, records) in selected.iter().zip(records) {
            let rows = match (data_type, records) {
                (DataType::Trade, EquityRecords::Trades(trades)) => {
                    trade_rows(trades, symbol, session)?
                }
                (DataType::Quote, EquityRecords::Quotes(quotes)) => {
                    quote_rows(quotes, symbol, session)?
                }
                _ => unreachable!("fetch results align with the selected data types"),
            };

            metadata.push(self.writer.write(
                &rows,
                session.session_date,
                AssetClass::Equity,
                *data_type,
                None,
                symbol,
            )?);
        }
        Ok(StagedEquityObjects {
            objects: self.writer.drain_staged(),
            metadata,
        })
    }

    /// Publish error coverage for every selected data type.
    fn publish_error(
        &self,
        symbol: &str,
        session: &CoverageSession,
        selected: &[DataType],
        message: String,
        retryable: bool,
    ) -> Result<EquityIngestResult, DataError> {
        let coverage = selected
            .iter()
            .map(|data_type| {
                CoverageRecord::new(
                    AssetClass::Equity,
                    *data_type,
                    symbol,
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
        Ok(EquityIngestResult {
            symbol: symbol.to_string(),
            session_date: session.session_date,
            coverage,
            objects: Vec::new(),
            retryable,
        })
    }
}

/// Fetch one selected dataset with the transient retry budget applied.
async fn fetch_records(
    provider: &dyn EquityDataProvider,
    data_type: DataType,
    symbol: &str,
    session: &CoverageSession,
    retry: TransientRetry,
) -> Result<EquityRecords, ProviderError> {
    let symbols = [symbol.to_string()];
    match data_type {
        DataType::Trade => fetch_with_transient_retry(retry, || {
            provider.fetch_trades(&symbols, session.open_time, session.close_time)
        })
        .await
        .map(EquityRecords::Trades),
        DataType::Quote => fetch_with_transient_retry(retry, || {
            provider.fetch_quotes(&symbols, session.open_time, session.close_time)
        })
        .await
        .map(EquityRecords::Quotes),
        other => Err(ProviderError::Configuration(format!(
            "unsupported equity data type: {}",
            other.value()
        ))),
    }
}

/// Build the coverage rows for one staged transaction.
fn coverage_records(
    symbol: &str,
    session: &CoverageSession,
    selected: &[DataType],
    metadata: &[ParquetMetadata],
) -> Result<Vec<CoverageRecord>, DataError> {
    selected
        .iter()
        .zip(metadata)
        .map(|(data_type, metadata)| {
            CoverageRecord::new(
                AssetClass::Equity,
                *data_type,
                symbol,
                session.session_date,
                coverage_status(session.is_partial, metadata.rows),
                Some(metadata.path.clone()),
                metadata.rows,
                metadata.bytes,
                Some(metadata.checksum.clone()),
                None,
                None,
            )
        })
        .collect()
}

/// Return the coverage status for one data type.
///
/// A closed session with rows is complete and a closed session with none is
/// empty; only a session that is still trading is partial.
fn coverage_status(session_partial: bool, rows: u64) -> CoverageStatus {
    if session_partial {
        CoverageStatus::Partial
    } else if rows > 0 {
        CoverageStatus::Complete
    } else {
        CoverageStatus::Empty
    }
}

/// Validate and canonically order equity trades.
fn trade_rows(
    records: &[NormalizedTrade],
    symbol: &str,
    session: &CoverageSession,
) -> Result<RecordBatch, DataError> {
    let ordered = ordered_records(
        records,
        |record| validate_trade(record, symbol, session),
        compare_trades,
    )?;
    crate::columnar::equity_trades(&ordered, symbol)
}

/// Validate and canonically order equity quotes.
fn quote_rows(
    records: &[NormalizedQuote],
    symbol: &str,
    session: &CoverageSession,
) -> Result<RecordBatch, DataError> {
    let ordered = ordered_records(
        records,
        |record| validate_quote(record, symbol, session),
        compare_quotes,
    )?;
    crate::columnar::equity_quotes(&ordered, symbol)
}

/// Compare two trades on the pinned `(timestamp, trade_id, exchange, price,
/// size, conditions, tape, update or "")` order.
pub(crate) fn compare_trades(left: &NormalizedTrade, right: &NormalizedTrade) -> Ordering {
    left.timestamp
        .cmp(&right.timestamp)
        .then_with(|| left.trade_id.cmp(&right.trade_id))
        .then_with(|| left.exchange.cmp(&right.exchange))
        .then_with(|| left.price.cmp(&right.price))
        .then_with(|| left.size.cmp(&right.size))
        .then_with(|| left.conditions.cmp(&right.conditions))
        .then_with(|| left.tape.cmp(&right.tape))
        .then_with(|| {
            left.update
                .as_deref()
                .unwrap_or("")
                .cmp(right.update.as_deref().unwrap_or(""))
        })
}

/// Compare two quotes on the pinned `(timestamp, bid_exchange, bid_price,
/// bid_size, ask_exchange, ask_price, ask_size, conditions, tape)` order.
fn compare_quotes(left: &NormalizedQuote, right: &NormalizedQuote) -> Ordering {
    left.timestamp
        .cmp(&right.timestamp)
        .then_with(|| left.bid_exchange.cmp(&right.bid_exchange))
        .then_with(|| left.bid_price.cmp(&right.bid_price))
        .then_with(|| left.bid_size.cmp(&right.bid_size))
        .then_with(|| left.ask_exchange.cmp(&right.ask_exchange))
        .then_with(|| left.ask_price.cmp(&right.ask_price))
        .then_with(|| left.ask_size.cmp(&right.ask_size))
        .then_with(|| left.conditions.cmp(&right.conditions))
        .then_with(|| left.tape.cmp(&right.tape))
}

/// Validate one trade against the requested symbol and session window.
fn validate_trade(
    record: &NormalizedTrade,
    symbol: &str,
    session: &CoverageSession,
) -> Result<(), DataError> {
    validate_record_timestamp(record.timestamp, symbol, session)?;
    if record.symbol != symbol && record.symbol.trim().to_uppercase() != symbol {
        return Err(DataError::value(
            "trade symbol does not match the requested symbol",
        ));
    }
    Ok(())
}

/// Validate one quote against the requested symbol and session window.
fn validate_quote(
    record: &NormalizedQuote,
    symbol: &str,
    session: &CoverageSession,
) -> Result<(), DataError> {
    validate_record_timestamp(record.timestamp, symbol, session)?;
    if record.symbol != symbol && record.symbol.trim().to_uppercase() != symbol {
        return Err(DataError::value(
            "quote symbol does not match the requested symbol",
        ));
    }
    Ok(())
}

/// Validate one record timestamp against the inclusive session window.
///
/// Both session bounds are inclusive: the provider fetch passes `close_time`
/// as Alpaca's inclusive end, so the closing auction print stamped exactly at
/// 16:00:00 ET belongs to this session and must not be rejected.
fn validate_record_timestamp(
    timestamp: lake_contracts::DateTime,
    symbol: &str,
    session: &CoverageSession,
) -> Result<(), DataError> {
    if timestamp < session.open_time || timestamp > session.close_time {
        return Err(DataError::value(format!(
            "{symbol} record timestamp is outside session {}",
            session.session_date
        )));
    }
    Ok(())
}
