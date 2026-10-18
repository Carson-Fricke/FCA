//! Dependency-injected option symbol-day ingestion.
//!
//! Ports `lake.ingestion.options.transaction`. Option work is deliberately
//! kept at the OSI symbol-day boundary: the option client can batch provider
//! requests internally, while each selected contract gets deterministic trade
//! and quote objects plus matching coverage rows. Job execution and ownership
//! belong to the finite ACA worker, outside this transaction.
//!
//! Semantics preserved exactly:
//!
//! 1. **Failure atomicity** — a fetch, validation, or write failure publishes
//!    error coverage for every selected data type and no object, never a
//!    partial success; the committer is called exactly once per run.
//! 2. **Inclusive session bounds** — the fetch window and record validation
//!    both include the session close, because Alpaca's `end` parameter is
//!    inclusive and the closing print belongs to its session.
//! 3. **Closed empty sessions are terminal** — an empty response from a closed
//!    session is written as a valid empty Parquet object and cataloged as
//!    terminal `empty` coverage; a later delivery replaces the same paths and
//!    catalog keys instead of appending.

use std::cmp::Ordering;
use std::sync::Arc;

use arrow_array::RecordBatch;
use futures_util::future::{join_all, BoxFuture};
use lake_catalog::{ContractRecord, CoverageRecord, CoverageStatus};
use lake_contracts::{AssetClass, DataError, DataType, Date, DateTime, Decimal18};
use lake_coverage::CoverageSession;
use lake_providers::{NormalizedOptionQuote, NormalizedOptionTrade, ProviderError};
use lake_storage::ParquetMetadata;

use crate::common::{
    fetch_error_message, AtomicIngestCommitter, PartitionWriter, StagedParquetObject,
};

/// Provider boundary for selected OPRA contracts.
///
/// The Python transaction accepts a duck-typed client with
/// `get_option_trades` / `get_option_quotes` coroutines; the Rust port names
/// that boundary as an object-safe trait so the real [`AlpacaClient`] and test
/// fakes are interchangeable.
pub trait OptionMarketDataClient: Send + Sync {
    /// Fetch normalized trades for selected contracts and a session.
    fn get_option_trades<'a>(
        &'a self,
        contracts: &'a [ContractRecord],
        start: DateTime,
        end: DateTime,
    ) -> lake_providers::RecordStream<'a, NormalizedOptionTrade>;

    /// Fetch normalized quotes for selected contracts and a session.
    fn get_option_quotes<'a>(
        &'a self,
        contracts: &'a [ContractRecord],
        start: DateTime,
        end: DateTime,
    ) -> lake_providers::RecordStream<'a, NormalizedOptionQuote>;
}

/// Published metadata for one OSI contract and session transaction.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OptionIngestResult {
    /// The canonical OSI contract the transaction published.
    pub contract: ContractRecord,
    /// Session date the transaction published.
    pub session_date: Date,
    /// One coverage row per selected data type, in canonical order.
    pub coverage: Vec<CoverageRecord>,
    /// One object metadata entry per selected data type, in canonical order.
    pub objects: Vec<ParquetMetadata>,
}

impl OptionIngestResult {
    /// The canonical OSI symbol.
    pub fn symbol(&self) -> &str {
        &self.contract.symbol
    }
}

/// Fetch and atomically publish one selected OSI symbol-day.
#[derive(Clone)]
pub struct OptionIngestTransaction {
    client: Arc<dyn OptionMarketDataClient>,
    writer: Arc<dyn PartitionWriter>,
    committer: Arc<dyn AtomicIngestCommitter>,
}

/// Staged outputs of one transaction, before commit.
struct StagedOptionObjects {
    objects: Vec<StagedParquetObject>,
    metadata: Vec<ParquetMetadata>,
}

impl OptionIngestTransaction {
    /// Build a transaction over injected client, writer, and committer.
    pub fn new(
        client: Arc<dyn OptionMarketDataClient>,
        writer: Arc<dyn PartitionWriter>,
        committer: Arc<dyn AtomicIngestCommitter>,
    ) -> Self {
        Self {
            client,
            writer,
            committer,
        }
    }

    /// Fetch and publish the requested option datasets for one session.
    ///
    /// Empty responses from a closed session are written as valid empty
    /// Parquet objects and cataloged as terminal `empty` coverage. A later
    /// delivery publishes new immutable objects and atomically replaces current coverage.
    pub async fn run(
        &self,
        contract: ContractRecord,
        session: &CoverageSession,
        data_types: Option<&[DataType]>,
    ) -> Result<OptionIngestResult, DataError> {
        let session_memory = lake_pipeline::Pipeline::global().session().await?;
        let requested = normalize_data_types(data_types)?;
        let fetches: Vec<BoxFuture<'_, Result<ParquetMetadata, ProviderError>>> = requested
            .iter()
            .map(|data| {
                let contract = &contract;
                let data = *data;
                Box::pin(async move { self.stage_dataset(data, contract, session).await })
                    as BoxFuture<'_, _>
            })
            .collect();
        let results = join_all(fetches).await;

        if let Some(message) = fetch_error_message(&requested, &results) {
            let transaction = self.clone();
            let session = *session;
            return crate::common::blocking_stage(move || {
                let _session_memory = session_memory;
                transaction.publish_error(&contract, &session, &requested, message)
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
            transaction.finish(contract, &session, requested, metadata)
        })
        .await
    }

    /// Stage and publish on a bounded blocking thread, leaving fetch I/O free.
    fn finish(
        &self,
        contract: ContractRecord,
        session: &CoverageSession,
        requested: Vec<DataType>,
        metadata: Vec<ParquetMetadata>,
    ) -> Result<OptionIngestResult, DataError> {
        let staged = StagedOptionObjects {
            objects: self.writer.drain_staged(),
            metadata,
        };

        let coverage = match coverage_records(&contract, session, &requested, &staged.metadata) {
            Ok(coverage) => coverage,
            Err(error) => {
                self.writer.reset_staged();
                return Err(error);
            }
        };
        let outcome = self.committer.commit(&staged.objects, &coverage);
        self.writer.reset_staged();
        outcome?;
        Ok(OptionIngestResult {
            contract,
            session_date: session.session_date,
            coverage,
            objects: staged.metadata,
        })
    }

    async fn stage_dataset(
        &self,
        data: DataType,
        contract: &ContractRecord,
        session: &CoverageSession,
    ) -> Result<ParquetMetadata, ProviderError> {
        let contracts = std::slice::from_ref(contract);
        let identity = contract.clone();
        let day = *session;
        let spec = crate::acquisition::StagingSpec {
            asset: AssetClass::Option,
            data,
            day: session.session_date,
            symbol: contract.symbol.clone(),
        };
        match data {
            DataType::Trade => {
                crate::acquisition::stage_pages(
                    self.client
                        .get_option_trades(contracts, session.open_time, session.close_time),
                    move |rows| trade_rows(&rows, &identity, &day),
                    self.writer.clone(),
                    spec,
                )
                .await
            }
            DataType::Quote => {
                crate::acquisition::stage_pages(
                    self.client
                        .get_option_quotes(contracts, session.open_time, session.close_time),
                    move |rows| quote_rows(&rows, &identity, &day),
                    self.writer.clone(),
                    spec,
                )
                .await
            }
            _ => Err(ProviderError::Configuration(
                "unsupported option tick dataset".into(),
            )),
        }
    }

    /// Publish error coverage for every selected data type.
    fn publish_error(
        &self,
        contract: &ContractRecord,
        session: &CoverageSession,
        data_types: &[DataType],
        message: String,
    ) -> Result<OptionIngestResult, DataError> {
        self.writer.reset_staged();
        let coverage = data_types
            .iter()
            .map(|data_type| {
                CoverageRecord::new(
                    AssetClass::Option,
                    *data_type,
                    &contract.symbol,
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
        Ok(OptionIngestResult {
            contract: contract.clone(),
            session_date: session.session_date,
            coverage,
            objects: Vec::new(),
        })
    }
}

/// Return the requested data types in canonical trade-then-quote order.
///
/// `None` selects both data types. An empty selection or one containing
/// anything but trade and quote is rejected with the Python wording, and the
/// rejection happens before any fetch so no provider request is wasted.
pub(crate) fn normalize_data_types(
    values: Option<&[DataType]>,
) -> Result<Vec<DataType>, DataError> {
    const CANONICAL: [DataType; 2] = [DataType::Trade, DataType::Quote];
    let Some(values) = values else {
        return Ok(CANONICAL.to_vec());
    };
    if values.is_empty() {
        return Err(DataError::value("data_types must not be empty"));
    }
    if values.iter().any(|value| !CANONICAL.contains(value)) {
        return Err(DataError::value(
            "option data_types must be trades or quotes",
        ));
    }
    Ok(CANONICAL
        .into_iter()
        .filter(|data_type| values.contains(data_type))
        .collect())
}

/// Build the coverage rows for one staged transaction.
///
/// The row count is the number of fetched records, exactly like the Python
/// transaction: every fetched record is validated, so the staged object holds
/// the same number of rows. A closed session with no records is terminal
/// `empty`; only a session that is still trading is `partial`.
fn coverage_records(
    contract: &ContractRecord,
    session: &CoverageSession,
    data_types: &[DataType],
    metadata: &[ParquetMetadata],
) -> Result<Vec<CoverageRecord>, DataError> {
    data_types
        .iter()
        .zip(metadata)
        .map(|(data_type, metadata)| {
            let row_count = metadata.rows;
            CoverageRecord::new(
                AssetClass::Option,
                *data_type,
                &contract.symbol,
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
    } else if rows == 0 {
        CoverageStatus::Empty
    } else {
        CoverageStatus::Complete
    }
}

/// Validate and canonically order option trades.
fn trade_rows(
    records: &[NormalizedOptionTrade],
    contract: &ContractRecord,
    session: &CoverageSession,
) -> Result<RecordBatch, DataError> {
    let mut validated = Vec::with_capacity(records.len());
    for record in records {
        validate_record(
            record.timestamp,
            &record.symbol,
            &record.underlying,
            record.expiration_date,
            record.strike_price,
            record.option_type,
            contract,
            session,
        )?;
        validated.push(record);
    }
    validated.sort_by(|left, right| compare_option_trades(left, right));
    crate::columnar::option_trades(&validated, &contract.symbol)
}

/// Validate and canonically order option quotes.
fn quote_rows(
    records: &[NormalizedOptionQuote],
    contract: &ContractRecord,
    session: &CoverageSession,
) -> Result<RecordBatch, DataError> {
    let mut validated = Vec::with_capacity(records.len());
    for record in records {
        validate_record(
            record.timestamp,
            &record.symbol,
            &record.underlying,
            record.expiration_date,
            record.strike_price,
            record.option_type,
            contract,
            session,
        )?;
        validated.push(record);
    }
    validated.sort_by(|left, right| compare_option_quotes(left, right));
    crate::columnar::option_quotes(&validated, &contract.symbol)
}

/// Compare two option trades on the pinned `(timestamp, exchange, price, size,
/// condition)` order.
pub(crate) fn compare_option_trades(
    left: &NormalizedOptionTrade,
    right: &NormalizedOptionTrade,
) -> Ordering {
    left.timestamp
        .cmp(&right.timestamp)
        .then_with(|| left.exchange.cmp(&right.exchange))
        .then_with(|| left.price.cmp(&right.price))
        .then_with(|| left.size.cmp(&right.size))
        .then_with(|| left.condition.cmp(&right.condition))
}

/// Compare two option quotes on the pinned `(timestamp, bid_exchange,
/// bid_price, bid_size, ask_exchange, ask_price, ask_size, condition)` order.
pub(crate) fn compare_option_quotes(
    left: &NormalizedOptionQuote,
    right: &NormalizedOptionQuote,
) -> Ordering {
    left.timestamp
        .cmp(&right.timestamp)
        .then_with(|| left.bid_exchange.cmp(&right.bid_exchange))
        .then_with(|| left.bid_price.cmp(&right.bid_price))
        .then_with(|| left.bid_size.cmp(&right.bid_size))
        .then_with(|| left.ask_exchange.cmp(&right.ask_exchange))
        .then_with(|| left.ask_price.cmp(&right.ask_price))
        .then_with(|| left.ask_size.cmp(&right.ask_size))
        .then_with(|| left.condition.cmp(&right.condition))
}

/// Validate one record against the requested contract and session window.
#[allow(clippy::too_many_arguments)]
fn validate_record(
    timestamp: DateTime,
    symbol: &str,
    underlying: &str,
    expiration_date: Date,
    strike_price: Decimal18,
    option_type: lake_catalog::OptionType,
    contract: &ContractRecord,
    session: &CoverageSession,
) -> Result<(), DataError> {
    // Both session bounds are inclusive: the provider fetch passes close_time
    // as Alpaca's inclusive end, so the closing auction print stamped exactly
    // at session close belongs to this session and must not be rejected.
    if timestamp < session.open_time || timestamp > session.close_time {
        return Err(DataError::value(format!(
            "{} record timestamp is outside session {}",
            contract.symbol, session.session_date
        )));
    }
    if symbol != contract.symbol && symbol.trim().to_uppercase() != contract.symbol {
        return Err(DataError::value(
            "option record symbol does not match the requested symbol",
        ));
    }
    if underlying != contract.underlying {
        return Err(DataError::value(
            "option record underlying does not match the contract",
        ));
    }
    if expiration_date != contract.expiration_date {
        return Err(DataError::value(
            "option record expiration does not match the contract",
        ));
    }
    if strike_price != contract.strike_price {
        return Err(DataError::value(
            "option record strike does not match the contract",
        ));
    }
    if option_type != contract.option_type {
        return Err(DataError::value(
            "option record type does not match the contract",
        ));
    }
    Ok(())
}
