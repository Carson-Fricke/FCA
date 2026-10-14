//! Shared local/hosted ensure execution over provider and persistence ports.
use std::sync::Arc;
use std::time::Duration;

use futures_util::{future::BoxFuture, stream, StreamExt};
use lake_catalog::{ContractRepository, CoverageRecord, CoverageStatus};
use lake_contracts::{AssetClass, BarInterval, DataError, DataType, DateTime};
use lake_coverage::{CoverageSession, EnsureJob};

use crate::{
    AtomicIngestCommitter, BarsDataProvider, BarsIngestTransaction, CryptoDataProvider,
    CryptoIngestTransaction, EquityDataProvider, EquityIngestTransaction, OptionIngestTransaction,
    OptionMarketDataClient, StagingPartitionWriter, TradePartitionReader,
};

/// Infrastructure supplies providers; ensure policy stays independent of deployment.
#[derive(Default)]
pub struct EnsureProviders {
    pub equity: Option<Arc<dyn EquityDataProvider>>,
    pub crypto: Option<Arc<dyn CryptoDataProvider>>,
    pub bars: Option<Arc<dyn BarsDataProvider>>,
    pub options: Option<Arc<dyn OptionMarketDataClient>>,
}

/// One completed session, delivered after its atomic publication.
pub type EnsureProgress = Arc<dyn Fn(usize, usize, &CoverageRecord) + Send + Sync>;

/// Executes only the accepted gap plan, with exact datasets and bounded sessions.
pub struct EnsureIngest {
    pub providers: EnsureProviders,
    pub contracts: Option<Arc<dyn ContractRepository>>,
    pub committer: Arc<dyn AtomicIngestCommitter>,
    pub trades: Arc<dyn TradePartitionReader>,
    pub clock: Arc<dyn Fn() -> DateTime + Send + Sync>,
}

impl EnsureIngest {
    /// No job settlement occurs here: coverage application owns durable job state.
    pub async fn run(
        &self,
        job: &EnsureJob,
        progress: Option<EnsureProgress>,
    ) -> Result<(), DataError> {
        let request = job.request.canonical();
        request
            .validate_interval()
            .map_err(|e| DataError::value(e.to_string()))?;
        let mut sessions = Vec::new();
        for gap in &job.gaps {
            if gap.asset_class != request.asset_class
                || gap.data_type != request.data_type
                || gap.symbol != request.symbol
            {
                return Err(DataError::value("ensure gap does not match its request"));
            }
            if gap.is_actionable() {
                sessions.push(gap.session);
            }
        }
        sessions.sort_by_key(|session| session.session_date);
        sessions.dedup_by_key(|session| session.session_date);
        if sessions.is_empty() {
            return Ok(());
        }
        let total = sessions.len();
        let mut failures = Vec::new();
        if request.data_type == DataType::Bars {
            let provider = self
                .providers
                .bars
                .clone()
                .ok_or_else(|| DataError::value("bars provider is not configured"))?;
            let interval =
                BarInterval::parse(request.interval.as_deref().expect("validated interval"))?;
            let transaction = BarsIngestTransaction::new(
                provider,
                self.trades.clone(),
                Arc::new(StagingPartitionWriter::new()),
                self.committer.clone(),
            );
            let mut outcome = transaction
                .run(request.asset_class, &request.symbol, interval, &sessions)
                .await?;
            if outcome.retryable {
                let retry: Vec<_> = sessions
                    .iter()
                    .filter(|session| {
                        outcome.coverage.iter().any(|record| {
                            record.session_date == session.session_date
                                && record.status == CoverageStatus::Error
                        })
                    })
                    .copied()
                    .collect();
                if !retry.is_empty() {
                    tokio::time::sleep(Duration::from_secs(2)).await;
                    let retried = transaction
                        .run(request.asset_class, &request.symbol, interval, &retry)
                        .await?;
                    for record in retried.coverage {
                        outcome
                            .coverage
                            .retain(|old| old.session_date != record.session_date);
                        outcome.coverage.push(record);
                    }
                }
            }
            for (done, record) in outcome.coverage.iter().enumerate() {
                collect_failure(record, &mut failures);
                if let Some(sink) = &progress {
                    sink(total, done + 1, record);
                }
            }
        } else {
            let futures: Vec<BoxFuture<'_, Result<CoverageRecord, DataError>>> = sessions
                .into_iter()
                .map(|session| {
                    let request = &request;
                    Box::pin(async move {
                        self.run_session(
                            request.asset_class,
                            &request.symbol,
                            request.data_type,
                            &session,
                        )
                        .await
                    }) as BoxFuture<'_, Result<CoverageRecord, DataError>>
                })
                .collect();
            let mut work = stream::iter(futures).buffer_unordered(4);
            let mut done = 0;
            while let Some(result) = work.next().await {
                done += 1;
                match result {
                    Ok(record) => {
                        collect_failure(&record, &mut failures);
                        if let Some(sink) = &progress {
                            sink(total, done, &record);
                        }
                    }
                    Err(error) => failures.push(error.to_string()),
                }
            }
        }
        if failures.is_empty() {
            Ok(())
        } else {
            Err(DataError::EnsureJob(failures.join("; ")))
        }
    }

    async fn run_session(
        &self,
        asset: AssetClass,
        symbol: &str,
        data_type: DataType,
        session: &CoverageSession,
    ) -> Result<CoverageRecord, DataError> {
        for attempt in 0..2 {
            // Each concurrent session owns its staging buffer; commits may share a store.
            let writer = Arc::new(StagingPartitionWriter::new());
            let (coverage, retryable) =
                match asset {
                    AssetClass::Equity => {
                        let provider =
                            self.providers.equity.clone().ok_or_else(|| {
                                DataError::value("equity provider is not configured")
                            })?;
                        let result =
                            EquityIngestTransaction::new(provider, writer, self.committer.clone())
                                .run(symbol, session, Some(&[data_type]))
                                .await?;
                        (result.coverage, result.retryable)
                    }
                    AssetClass::Crypto => {
                        let provider =
                            self.providers.crypto.clone().ok_or_else(|| {
                                DataError::value("crypto provider is not configured")
                            })?;
                        let result =
                            CryptoIngestTransaction::new(provider, writer, self.committer.clone())
                                .with_clock(self.clock.clone())
                                .run(symbol, session, Some(&[data_type]))
                                .await?;
                        (result.coverage, result.retryable)
                    }
                    AssetClass::Option => {
                        let provider =
                            self.providers.options.clone().ok_or_else(|| {
                                DataError::value("option provider is not configured")
                            })?;
                        let contract = self
                            .contracts
                            .as_ref()
                            .ok_or_else(|| DataError::value("option contracts are not configured"))?
                            .get(symbol)?
                            .ok_or_else(|| {
                                DataError::NotFound("resolve option before ensure".into())
                            })?;
                        let result =
                            OptionIngestTransaction::new(provider, writer, self.committer.clone())
                                .run(contract, session, Some(&[data_type]))
                                .await?;
                        (result.coverage, false)
                    }
                    _ => return Err(DataError::value("unsupported ensure asset class")),
                };
            let record = coverage
                .into_iter()
                .next()
                .ok_or_else(|| DataError::value("ingest did not publish requested coverage"))?;
            if attempt == 0 && retryable && record.status == CoverageStatus::Error {
                tokio::time::sleep(Duration::from_secs(2)).await;
            } else {
                return Ok(record);
            }
        }
        unreachable!("last attempt returns its coverage")
    }
}

fn collect_failure(record: &CoverageRecord, failures: &mut Vec<String>) {
    if record.status == CoverageStatus::Error {
        failures.push(
            record
                .error_message
                .clone()
                .unwrap_or_else(|| "ingest failed".into()),
        );
    }
}
