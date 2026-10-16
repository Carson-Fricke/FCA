//! Full transaction round trips through the canonical writer and verified reader.

use std::sync::{Arc, Mutex};

use arrow_array::{
    Array, Decimal128Array, Int64Array, ListArray, RecordBatch, StringArray, UInt64Array,
};
use futures_util::future::BoxFuture;
use harness_market_reader::{
    CacheConfig, MarketDataReader, MarketDataSnapshot, Partition, PartitionSource, ReaderError,
    SnapshotRequest,
};
use lake_catalog::{ContractRecord, CoverageRecord, CoverageStatus, OptionType};
use lake_contracts::{schema_for, AssetClass, DataError, DataType, Date, DateTime, Decimal18};
use lake_coverage::CoverageSession;
use lake_ingestion::{
    AtomicIngestCommitter, CryptoDataProvider, CryptoIngestTransaction, EquityDataProvider,
    EquityIngestTransaction, OptionIngestTransaction, OptionMarketDataClient, StagedParquetObject,
    StagingPartitionWriter,
};
use lake_providers::{
    NormalizedCryptoQuote, NormalizedCryptoTrade, NormalizedOptionQuote, NormalizedOptionTrade,
    NormalizedQuote, NormalizedTrade, ProviderError,
};

fn decimal(value: &str) -> Decimal18 {
    Decimal18::parse(value).unwrap()
}
fn date() -> Date {
    Date::from_ymd(2025, 12, 30).unwrap()
}
fn session() -> CoverageSession {
    CoverageSession {
        session_date: date(),
        open_time: DateTime::midnight(date()),
        close_time: DateTime::midnight(date().add_days(1)),
        is_partial: false,
    }
}
fn contract() -> ContractRecord {
    ContractRecord::new(
        "AAPL260116C00200000",
        "AAPL",
        Date::from_ymd(2026, 1, 16).unwrap(),
        decimal("200"),
        OptionType::Call,
        100,
        None,
        None,
    )
    .unwrap()
}

struct Provider;
impl EquityDataProvider for Provider {
    fn fetch_trades<'a>(
        &'a self,
        _: &'a [String],
        start: DateTime,
        _: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedTrade>, ProviderError>> {
        Box::pin(async move {
            Ok([u64::MAX, 1]
                .into_iter()
                .map(|trade_id| NormalizedTrade {
                    timestamp: start,
                    symbol: "aapl".into(),
                    trade_id,
                    exchange: "X".into(),
                    price: decimal("190.123456789012345678"),
                    size: u32::MAX,
                    conditions: vec!["@".into(), "I".into()],
                    tape: "C".into(),
                    update: None,
                })
                .collect())
        })
    }
    fn fetch_quotes<'a>(
        &'a self,
        _: &'a [String],
        start: DateTime,
        _: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedQuote>, ProviderError>> {
        Box::pin(async move {
            Ok(vec![NormalizedQuote {
                timestamp: start,
                symbol: "AAPL".into(),
                bid_exchange: "X".into(),
                bid_price: decimal("190.000000000000000001"),
                bid_size: 10,
                ask_exchange: "Y".into(),
                ask_price: decimal("190.2"),
                ask_size: 20,
                conditions: Vec::new(),
                tape: "C".into(),
            }])
        })
    }
}
impl CryptoDataProvider for Provider {
    fn fetch_crypto_trades<'a>(
        &'a self,
        _: &'a [String],
        start: DateTime,
        _: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedCryptoTrade>, ProviderError>> {
        Box::pin(async move {
            Ok(vec![NormalizedCryptoTrade {
                timestamp: start,
                symbol: "BTC/USD".into(),
                trade_id: None,
                exchange: None,
                price: decimal("95000.123456789012345678"),
                size: decimal("0.000000000000000001"),
                taker_side: None,
            }])
        })
    }
    fn fetch_crypto_quotes<'a>(
        &'a self,
        _: &'a [String],
        start: DateTime,
        _: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedCryptoQuote>, ProviderError>> {
        Box::pin(async move {
            Ok(vec![NormalizedCryptoQuote {
                timestamp: start,
                symbol: "BTC/USD".into(),
                bid_exchange: None,
                bid_price: decimal("95000.1"),
                bid_size: decimal("0.000000000000000001"),
                ask_exchange: Some("CBSE".into()),
                ask_price: decimal("95001.2"),
                ask_size: decimal("0.2"),
            }])
        })
    }
}
impl OptionMarketDataClient for Provider {
    fn get_option_trades<'a>(
        &'a self,
        contracts: &'a [ContractRecord],
        start: DateTime,
        _: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedOptionTrade>, ProviderError>> {
        Box::pin(async move {
            let c = &contracts[0];
            Ok(vec![NormalizedOptionTrade {
                timestamp: start,
                symbol: c.symbol.clone(),
                underlying: c.underlying.clone(),
                expiration_date: c.expiration_date,
                strike_price: c.strike_price,
                option_type: c.option_type,
                exchange: "Q".into(),
                price: decimal("2.000000000000000001"),
                size: 3,
                condition: "@".into(),
            }])
        })
    }
    fn get_option_quotes<'a>(
        &'a self,
        contracts: &'a [ContractRecord],
        start: DateTime,
        _: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedOptionQuote>, ProviderError>> {
        Box::pin(async move {
            let c = &contracts[0];
            Ok(vec![NormalizedOptionQuote {
                timestamp: start,
                symbol: c.symbol.clone(),
                underlying: c.underlying.clone(),
                expiration_date: c.expiration_date,
                strike_price: c.strike_price,
                option_type: c.option_type,
                bid_exchange: "Q".into(),
                bid_price: decimal("1.9"),
                bid_size: 3,
                ask_exchange: "Z".into(),
                ask_price: decimal("2.1"),
                ask_size: 4,
                condition: "R".into(),
            }])
        })
    }
}

#[derive(Default)]
struct Published(Mutex<Vec<StagedParquetObject>>);
impl AtomicIngestCommitter for Published {
    fn commit(
        &self,
        objects: &[StagedParquetObject],
        coverage: &[CoverageRecord],
    ) -> Result<(), DataError> {
        assert!(coverage
            .iter()
            .all(|row| row.status == CoverageStatus::Complete));
        self.0.lock().unwrap().extend_from_slice(objects);
        Ok(())
    }
}
impl PartitionSource for Published {
    fn read<'a>(
        &'a self,
        partition: &'a Partition,
    ) -> BoxFuture<'a, Result<bytes::Bytes, ReaderError>> {
        Box::pin(async move {
            Ok(bytes::Bytes::from(
                self.0
                    .lock()
                    .unwrap()
                    .iter()
                    .find(|o| o.metadata.path == partition.object_path)
                    .unwrap()
                    .payload
                    .clone(),
            ))
        })
    }
}
async fn read(
    published: &Published,
    asset: AssetClass,
    data_type: DataType,
    symbol: &str,
) -> RecordBatch {
    let metadata = published
        .0
        .lock()
        .unwrap()
        .iter()
        .find(|o| {
            o.metadata
                .path
                .contains(&format!("data_type={}/", data_type.value()))
                && o.metadata
                    .path
                    .contains(&format!("asset_class={}/", asset.value()))
        })
        .unwrap()
        .metadata
        .clone();
    let snapshot = MarketDataSnapshot::new(
        SnapshotRequest {
            asset_class: asset.value().into(),
            data_type: data_type.value().into(),
            symbol: symbol.into(),
            start_date: date().isoformat(),
            end_date: date().isoformat(),
            interval: None,
        },
        vec![Partition {
            session_date: date().isoformat(),
            object_path: metadata.path,
            checksum: metadata.checksum,
            row_count: metadata.rows,
            byte_size: metadata.bytes,
        }],
    )
    .unwrap();
    let temporary = tempfile::tempdir().unwrap();
    let reader = MarketDataReader::new(CacheConfig {
        directory: temporary.path().into(),
        disk_bytes: 0,
        memory_bytes: 0,
    });
    let mut batches = Vec::new();
    reader
        .visit(&snapshot, published, |batch| {
            batches.push(batch);
            Ok(())
        })
        .await
        .unwrap();
    let batch = batches.remove(0);
    assert_eq!(
        batch.schema().fields(),
        schema_for(asset, data_type).unwrap().fields()
    );
    let symbols = batch
        .column_by_name("symbol")
        .unwrap()
        .as_any()
        .downcast_ref::<StringArray>()
        .unwrap();
    assert!(symbols.iter().all(|value| value == Some(symbol)));
    batch
}
fn exact(batch: &RecordBatch, column: &str, value: &str) {
    assert_eq!(
        batch
            .column_by_name(column)
            .unwrap()
            .as_any()
            .downcast_ref::<Decimal128Array>()
            .unwrap()
            .value(0),
        decimal(value).unscaled()
    );
}

#[tokio::test]
async fn transactions_publish_exact_canonical_arrow_for_every_tick_schema() {
    let published = Arc::new(Published::default());
    let provider = Arc::new(Provider);
    EquityIngestTransaction::new(
        provider.clone(),
        Arc::new(StagingPartitionWriter::new()),
        published.clone(),
    )
    .run("AAPL", &session(), None)
    .await
    .unwrap();
    CryptoIngestTransaction::new(
        provider.clone(),
        Arc::new(StagingPartitionWriter::new()),
        published.clone(),
    )
    .run("BTC/USD", &session(), None)
    .await
    .unwrap();
    OptionIngestTransaction::new(
        provider,
        Arc::new(StagingPartitionWriter::new()),
        published.clone(),
    )
    .run(contract(), &session(), None)
    .await
    .unwrap();

    let trade = read(&published, AssetClass::Equity, DataType::Trade, "AAPL").await;
    exact(&trade, "price", "190.123456789012345678");
    let ids = trade
        .column_by_name("trade_id")
        .unwrap()
        .as_any()
        .downcast_ref::<UInt64Array>()
        .unwrap();
    assert_eq!(ids.values().as_ref(), &[1, u64::MAX]);
    assert!(trade.column_by_name("update").unwrap().is_null(0));
    let conditions = trade
        .column_by_name("conditions")
        .unwrap()
        .as_any()
        .downcast_ref::<ListArray>()
        .unwrap();
    let values = conditions.value(0);
    assert_eq!(
        values
            .as_any()
            .downcast_ref::<StringArray>()
            .unwrap()
            .iter()
            .collect::<Vec<_>>(),
        vec![Some("@"), Some("I")]
    );
    let quote = read(&published, AssetClass::Equity, DataType::Quote, "AAPL").await;
    exact(&quote, "bid_price", "190.000000000000000001");
    let crypto = read(&published, AssetClass::Crypto, DataType::Trade, "BTC/USD").await;
    exact(&crypto, "size", "0.000000000000000001");
    assert!(crypto
        .column_by_name("trade_id")
        .unwrap()
        .as_any()
        .downcast_ref::<Int64Array>()
        .unwrap()
        .is_null(0));
    assert!(crypto.column_by_name("exchange").unwrap().is_null(0));
    let crypto_quote = read(&published, AssetClass::Crypto, DataType::Quote, "BTC/USD").await;
    exact(&crypto_quote, "bid_size", "0.000000000000000001");
    assert!(crypto_quote
        .column_by_name("bid_exchange")
        .unwrap()
        .is_null(0));
    let option = read(
        &published,
        AssetClass::Option,
        DataType::Trade,
        &contract().symbol,
    )
    .await;
    exact(&option, "price", "2.000000000000000001");
    let option_quote = read(
        &published,
        AssetClass::Option,
        DataType::Quote,
        &contract().symbol,
    )
    .await;
    exact(&option_quote, "ask_price", "2.1");
}

struct InvalidQuotes;
impl EquityDataProvider for InvalidQuotes {
    fn fetch_trades<'a>(
        &'a self,
        symbols: &'a [String],
        start: DateTime,
        end: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedTrade>, ProviderError>> {
        Provider.fetch_trades(symbols, start, end)
    }

    fn fetch_quotes<'a>(
        &'a self,
        symbols: &'a [String],
        start: DateTime,
        end: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedQuote>, ProviderError>> {
        Box::pin(async move {
            let mut quotes = Provider.fetch_quotes(symbols, start, end).await?;
            quotes[0].symbol = "OTHER".into();
            Ok(quotes)
        })
    }
}

#[derive(Default)]
struct FailurePublication(Mutex<Vec<Vec<CoverageRecord>>>);
impl AtomicIngestCommitter for FailurePublication {
    fn commit(
        &self,
        objects: &[StagedParquetObject],
        coverage: &[CoverageRecord],
    ) -> Result<(), DataError> {
        assert!(
            objects.is_empty(),
            "invalid quotes must not publish staged trades"
        );
        self.0.lock().unwrap().push(coverage.to_vec());
        Ok(())
    }
}

#[tokio::test]
async fn invalid_quotes_publish_only_one_atomic_error_outcome_for_both_datasets() {
    let published = Arc::new(FailurePublication::default());
    let result = EquityIngestTransaction::new(
        Arc::new(InvalidQuotes),
        Arc::new(StagingPartitionWriter::new()),
        published.clone(),
    )
    .run("AAPL", &session(), None)
    .await
    .unwrap();
    assert!(result.objects.is_empty());
    assert_eq!(result.coverage.len(), 2);
    assert!(result
        .coverage
        .iter()
        .all(|row| row.status == CoverageStatus::Error));
    assert_eq!(*published.0.lock().unwrap(), vec![result.coverage]);
}

/// Simulates external publication waiting on I/O, while the provider serves
/// another independent session on the same single-thread async runtime.
struct Pipeline {
    fetched: std::sync::Condvar,
    count: Mutex<usize>,
    started: Mutex<Option<tokio::sync::oneshot::Sender<()>>>,
}

impl EquityDataProvider for Pipeline {
    fn fetch_trades<'a>(
        &'a self,
        symbols: &'a [String],
        start: DateTime,
        end: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedTrade>, ProviderError>> {
        Box::pin(async move {
            *self.count.lock().unwrap() += 1;
            self.fetched.notify_all();
            Provider.fetch_trades(symbols, start, end).await
        })
    }

    fn fetch_quotes<'a>(
        &'a self,
        symbols: &'a [String],
        start: DateTime,
        end: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedQuote>, ProviderError>> {
        Provider.fetch_quotes(symbols, start, end)
    }
}

impl AtomicIngestCommitter for Pipeline {
    fn commit(&self, _: &[StagedParquetObject], _: &[CoverageRecord]) -> Result<(), DataError> {
        if let Some(started) = self.started.lock().unwrap().take() {
            started.send(()).unwrap();
            let (count, _) = self
                .fetched
                .wait_timeout_while(
                    self.count.lock().unwrap(),
                    std::time::Duration::from_secs(2),
                    |count| *count < 2,
                )
                .unwrap();
            if *count < 2 {
                return Err(DataError::value(
                    "publication blocked the next session fetch",
                ));
            }
        }
        Ok(())
    }
}

#[tokio::test]
async fn next_session_fetch_progresses_while_another_session_publishes() {
    let (started, waiting) = tokio::sync::oneshot::channel();
    let pipeline = Arc::new(Pipeline {
        fetched: std::sync::Condvar::new(),
        count: Mutex::new(0),
        started: Mutex::new(Some(started)),
    });
    let first = EquityIngestTransaction::new(
        pipeline.clone(),
        Arc::new(StagingPartitionWriter::new()),
        pipeline.clone(),
    );
    let first = tokio::spawn(async move {
        first
            .run("AAPL", &session(), Some(&[DataType::Trade]))
            .await
    });
    tokio::time::timeout(std::time::Duration::from_secs(1), waiting)
        .await
        .expect("first session reaches publication")
        .unwrap();
    let mut next = session();
    next.session_date = next.session_date.add_days(1);
    next.open_time = DateTime::midnight(next.session_date);
    next.close_time = DateTime::midnight(next.session_date.add_days(1));
    EquityIngestTransaction::new(
        pipeline.clone(),
        Arc::new(StagingPartitionWriter::new()),
        pipeline,
    )
    .run("AAPL", &next, Some(&[DataType::Trade]))
    .await
    .expect("next session publishes");
    first
        .await
        .unwrap()
        .expect("first publication observes the next fetch");
}
