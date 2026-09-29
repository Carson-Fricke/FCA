//! Bars transaction behavior: provider-first serving, trade derivation,
//! provenance revisions, one atomic commit, and per-session error coverage.

mod support;

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

use futures_util::future::BoxFuture;
use lake_catalog::{BarSource, CoverageStatus};
use lake_contracts::{AssetClass, BarInterval, DataError, Date, DateTime, Decimal18};
use lake_coverage::CoverageSession;
use lake_ingestion::{
    BarsDataProvider, BarsIngestTransaction, StoredTrade, StoredTradePartition,
    TradePartitionReader,
};
use lake_providers::{NormalizedBar, NormalizedTrade, ProviderError};
use lake_storage::EquityTradeRecord;
use serde_json::Value;

use support::{
    crypto_day, day, equity_session, guard, instant, RecordingCommitter, RecordingWriter,
};

type BarsHandler = Box<
    dyn Fn(
            AssetClass,
            &str,
            BarInterval,
            DateTime,
            DateTime,
        ) -> Result<Vec<NormalizedBar>, ProviderError>
        + Send
        + Sync,
>;

/// Provider fake whose bars come from an injected handler.
struct FakeBarsProvider {
    handler: BarsHandler,
    calls: Mutex<Vec<(AssetClass, String, BarInterval, DateTime, DateTime)>>,
}

impl FakeBarsProvider {
    fn new(
        handler: impl Fn(
                AssetClass,
                &str,
                BarInterval,
                DateTime,
                DateTime,
            ) -> Result<Vec<NormalizedBar>, ProviderError>
            + Send
            + Sync
            + 'static,
    ) -> Arc<Self> {
        Arc::new(Self {
            handler: Box::new(handler),
            calls: Mutex::new(Vec::new()),
        })
    }

    fn returning(bars: Vec<NormalizedBar>) -> Arc<Self> {
        let bars = Arc::new(bars);
        Self::new(move |_, _, _, _, _| Ok((*bars).clone()))
    }

    fn calls(&self) -> Vec<(AssetClass, String, BarInterval, DateTime, DateTime)> {
        guard(&self.calls).clone()
    }
}

impl BarsDataProvider for FakeBarsProvider {
    fn fetch_bars<'a>(
        &'a self,
        asset_class: AssetClass,
        symbol: &'a str,
        interval: BarInterval,
        start: DateTime,
        end: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedBar>, ProviderError>> {
        guard(&self.calls).push((asset_class, symbol.to_string(), interval, start, end));
        let outcome = (self.handler)(asset_class, symbol, interval, start, end);
        Box::pin(async move { outcome })
    }
}

/// Trade reader fake backed by a fixed partition map.
#[derive(Default)]
struct FakeTradeReader {
    partitions: Mutex<BTreeMap<(AssetClass, String, Date), StoredTradePartition>>,
    calls: Mutex<Vec<(AssetClass, String, Date)>>,
}

impl FakeTradeReader {
    fn with_partition(
        &self,
        asset_class: AssetClass,
        symbol: &str,
        session_date: Date,
        partition: StoredTradePartition,
    ) {
        guard(&self.partitions).insert((asset_class, symbol.to_string(), session_date), partition);
    }

    fn calls(&self) -> Vec<(AssetClass, String, Date)> {
        guard(&self.calls).clone()
    }
}

impl TradePartitionReader for FakeTradeReader {
    fn read<'a>(
        &'a self,
        asset_class: AssetClass,
        symbol: &'a str,
        session_date: Date,
    ) -> BoxFuture<'a, Result<Option<StoredTradePartition>, DataError>> {
        guard(&self.calls).push((asset_class, symbol.to_string(), session_date));
        let partition = guard(&self.partitions)
            .get(&(asset_class, symbol.to_string(), session_date))
            .cloned();
        Box::pin(async move { Ok(partition) })
    }
}

fn bar(
    timestamp: DateTime,
    symbol: &str,
    open: &str,
    high: &str,
    low: &str,
    close: &str,
    volume: &str,
) -> NormalizedBar {
    NormalizedBar {
        timestamp,
        symbol: symbol.to_string(),
        open: Decimal18::parse(open).expect("open"),
        high: Decimal18::parse(high).expect("high"),
        low: Decimal18::parse(low).expect("low"),
        close: Decimal18::parse(close).expect("close"),
        volume: Decimal18::parse(volume).expect("volume"),
    }
}

fn stored_equity(revision: &str, trades: &[NormalizedTrade]) -> StoredTradePartition {
    StoredTradePartition {
        records: trades
            .iter()
            .map(|trade| {
                StoredTrade::Equity(EquityTradeRecord {
                    timestamp: trade.timestamp,
                    symbol: trade.symbol.clone(),
                    trade_id: trade.trade_id,
                    exchange: trade.exchange.clone(),
                    price: trade.price,
                    size: trade.size,
                    conditions: trade.conditions.clone(),
                    tape: trade.tape.clone(),
                    update: trade.update.clone(),
                })
            })
            .collect(),
        revision: revision.to_string(),
    }
}

fn trade(timestamp: DateTime, trade_id: u64, price: &str, size: u32) -> NormalizedTrade {
    NormalizedTrade {
        timestamp,
        symbol: "AAPL".to_string(),
        trade_id,
        exchange: "XNAS".to_string(),
        price: Decimal18::parse(price).expect("price"),
        size,
        conditions: vec!["@".to_string()],
        tape: "C".to_string(),
        update: None,
    }
}

fn at(year: i32, month: u32, day_of_month: u32, hour: u32, minute: u32) -> DateTime {
    DateTime::from_ymd_hms(year, month, day_of_month, hour, minute, 0, 0).expect("instant")
}

fn transaction(
    provider: Arc<FakeBarsProvider>,
    reader: Arc<FakeTradeReader>,
) -> (
    BarsIngestTransaction,
    Arc<RecordingWriter>,
    Arc<RecordingCommitter>,
) {
    let writer = Arc::new(RecordingWriter::new());
    let committer = Arc::new(RecordingCommitter::new());
    let transaction =
        BarsIngestTransaction::new(provider, reader, writer.clone(), committer.clone());
    (transaction, writer, committer)
}

#[tokio::test]
async fn provider_bars_win_and_record_provider_provenance() {
    let session = equity_session(false);
    let provider = FakeBarsProvider::returning(vec![
        bar(
            instant(14, 31, 0, 0),
            "AAPL",
            "190.7",
            "190.7",
            "190.5",
            "190.6",
            "5",
        ),
        bar(
            instant(14, 30, 0, 0),
            "AAPL",
            "189.9",
            "190.5",
            "189.9",
            "190.4",
            "30",
        ),
    ]);
    let reader = Arc::new(FakeTradeReader::default());
    let (transaction, writer, committer) = transaction(provider.clone(), reader.clone());

    let result = transaction
        .run(
            AssetClass::Equity,
            " aapl ",
            BarInterval::OneMinute,
            &[session],
        )
        .await
        .expect("run");

    assert_eq!(result.symbol, "AAPL");
    assert_eq!(result.interval, BarInterval::OneMinute);
    assert_eq!(result.coverage.len(), 1);
    let coverage = &result.coverage[0];
    assert_eq!(coverage.status, CoverageStatus::Complete);
    assert_eq!(coverage.bar_source, Some(BarSource::Provider));
    assert_eq!(coverage.source_revision, coverage.checksum);
    assert_eq!(coverage.interval.as_deref(), Some("1min"));
    assert!(coverage
        .object_path
        .as_deref()
        .expect("path")
        .contains("interval=1min"));
    assert!(reader.calls().is_empty(), "the provider served the session");

    let bar = instant(14, 30, 0, 0);
    assert_eq!(
        provider.calls(),
        vec![(
            AssetClass::Equity,
            "AAPL".to_string(),
            BarInterval::OneMinute,
            bar,
            instant(21, 0, 0, 0),
        )]
    );

    let writes = writer.writes();
    assert_eq!(writes.len(), 1);
    assert_eq!(writes[0].interval.as_deref(), Some("1min"));
    assert_eq!(writes[0].records[0]["timestamp"], bar.to_nanos());
    assert_eq!(
        writes[0].records[1]["timestamp"],
        instant(14, 31, 0, 0).to_nanos()
    );

    assert_eq!(committer.commit_count(), 1);
    assert_eq!(committer.commits()[0].0.len(), 1);
    assert_eq!(committer.commits()[0].1, result.coverage);
}

#[tokio::test]
async fn bars_outside_the_requested_sessions_are_excluded() {
    let session = equity_session(false);
    let provider = FakeBarsProvider::returning(vec![
        bar(
            instant(13, 0, 0, 0),
            "AAPL",
            "188.0",
            "188.5",
            "187.5",
            "188.2",
            "1",
        ),
        bar(
            instant(14, 30, 0, 0),
            "AAPL",
            "189.9",
            "190.5",
            "189.9",
            "190.4",
            "30",
        ),
    ]);
    let reader = Arc::new(FakeTradeReader::default());
    let (transaction, writer, _committer) = transaction(provider, reader);

    let result = transaction
        .run(
            AssetClass::Equity,
            "AAPL",
            BarInterval::OneMinute,
            &[session],
        )
        .await
        .expect("run");

    assert_eq!(result.coverage[0].status, CoverageStatus::Complete);
    let writes = writer.writes();
    assert_eq!(writes.len(), 1);
    assert_eq!(writes[0].records.len(), 1, "pre-market bar excluded");
    assert_eq!(
        writes[0].records[0]["open"],
        Value::String("189.9".to_string())
    );
}

#[tokio::test]
async fn provider_bars_colliding_in_one_bucket_surface_error_coverage() {
    let session = equity_session(false);
    let provider = FakeBarsProvider::returning(vec![
        bar(instant(14, 30, 0, 0), "AAPL", "1", "1", "1", "1", "1"),
        bar(instant(15, 0, 0, 0), "AAPL", "2", "2", "2", "2", "2"),
    ]);
    let reader = Arc::new(FakeTradeReader::default());
    let (transaction, writer, committer) = transaction(provider, reader);

    let result = transaction
        .run(AssetClass::Equity, "AAPL", BarInterval::OneDay, &[session])
        .await
        .expect("run");

    assert_eq!(result.coverage[0].status, CoverageStatus::Error);
    assert!(result.coverage[0]
        .error_message
        .as_deref()
        .expect("message")
        .contains("overlap a 1d bucket"));
    assert!(writer.writes().is_empty());
    assert_eq!(committer.commit_count(), 1, "error coverage still commits");
    assert_eq!(committer.commits()[0].0.len(), 0);
}

#[tokio::test]
async fn empty_provider_sessions_derive_from_stored_trades() {
    let session = equity_session(false);
    let provider = FakeBarsProvider::returning(Vec::new());
    let reader = Arc::new(FakeTradeReader::default());
    reader.with_partition(
        AssetClass::Equity,
        "AAPL",
        session.session_date,
        stored_equity(
            "trade-rev-1",
            &[
                trade(instant(14, 30, 30, 0), 2, "190.5", 5),
                trade(instant(14, 30, 0, 0), 1, "190.125", 100),
            ],
        ),
    );
    let (transaction, writer, _committer) = transaction(provider, reader.clone());

    let result = transaction
        .run(
            AssetClass::Equity,
            "AAPL",
            BarInterval::OneMinute,
            &[session],
        )
        .await
        .expect("run");

    let coverage = &result.coverage[0];
    assert_eq!(coverage.status, CoverageStatus::Complete);
    assert_eq!(coverage.bar_source, Some(BarSource::Derived));
    assert_eq!(coverage.source_revision.as_deref(), Some("trade-rev-1"));
    assert_eq!(coverage.row_count, 1);
    assert_eq!(reader.calls().len(), 1);

    let writes = writer.writes();
    assert_eq!(writes[0].records.len(), 1);
    let row = &writes[0].records[0];
    assert_eq!(row["timestamp"], instant(14, 30, 0, 0).to_nanos());
    assert_eq!(row["open"], Value::String("190.125".to_string()));
    assert_eq!(row["high"], Value::String("190.5".to_string()));
    assert_eq!(row["low"], Value::String("190.125".to_string()));
    assert_eq!(row["close"], Value::String("190.5".to_string()));
    assert_eq!(row["volume"], Value::String("105".to_string()));
}

#[tokio::test]
async fn permanently_unsupported_provider_ranges_derive_from_trades() {
    let session = equity_session(false);
    let provider = FakeBarsProvider::new(|_, _, _, _, _| {
        Err(ProviderError::Configuration(
            "unsupported old range".to_string(),
        ))
    });
    let reader = Arc::new(FakeTradeReader::default());
    reader.with_partition(
        AssetClass::Equity,
        "AAPL",
        session.session_date,
        stored_equity(
            "trade-rev-1",
            &[trade(instant(14, 30, 0, 0), 1, "190.125", 100)],
        ),
    );
    let (transaction, _writer, _committer) = transaction(provider, reader);

    let result = transaction
        .run(
            AssetClass::Equity,
            "AAPL",
            BarInterval::OneMinute,
            &[session],
        )
        .await
        .expect("run");

    assert_eq!(result.coverage[0].status, CoverageStatus::Complete);
    assert_eq!(
        result.coverage[0].bar_source,
        Some(BarSource::Derived),
        "a permanent provider failure derives instead of failing"
    );
    assert!(!result.retryable);
}

#[tokio::test]
async fn transient_provider_failure_without_trades_is_error_coverage() {
    let session = equity_session(false);
    let provider = FakeBarsProvider::new(|_, _, _, _, _| {
        Err(ProviderError::Transport(
            "temporary DNS failure".to_string(),
        ))
    });
    let reader = Arc::new(FakeTradeReader::default());
    let (transaction, writer, committer) = transaction(provider, reader);

    let result = transaction
        .run(
            AssetClass::Equity,
            "AAPL",
            BarInterval::OneMinute,
            &[session],
        )
        .await
        .expect("run");

    let coverage = &result.coverage[0];
    assert_eq!(coverage.status, CoverageStatus::Error);
    assert!(coverage
        .error_message
        .as_deref()
        .expect("message")
        .contains("TransportError"));
    assert_eq!(coverage.bar_source, None);
    assert!(result.retryable);
    assert!(writer.writes().is_empty());
    assert_eq!(committer.commit_count(), 1);
    assert_eq!(committer.commits()[0].0.len(), 0);
    assert_eq!(committer.commits()[0].1, result.coverage);
}

#[tokio::test]
async fn empty_sessions_write_a_valid_empty_partition_with_empty_coverage() {
    let session = equity_session(false);
    let provider = FakeBarsProvider::returning(Vec::new());
    let reader = Arc::new(FakeTradeReader::default());
    reader.with_partition(
        AssetClass::Equity,
        "AAPL",
        session.session_date,
        stored_equity("trade-rev-1", &[]),
    );
    let (transaction, writer, committer) = transaction(provider, reader);

    let result = transaction
        .run(
            AssetClass::Equity,
            "AAPL",
            BarInterval::OneMinute,
            &[session],
        )
        .await
        .expect("run");

    let coverage = &result.coverage[0];
    assert_eq!(coverage.status, CoverageStatus::Empty);
    assert_eq!(coverage.row_count, 0);
    assert_eq!(coverage.bar_source, Some(BarSource::Derived));
    assert_eq!(coverage.source_revision.as_deref(), Some("trade-rev-1"));
    assert!(coverage.object_path.is_some());

    let writes = writer.writes();
    assert_eq!(writes.len(), 1, "a valid empty parquet is still written");
    assert!(writes[0].records.is_empty());
    assert_eq!(committer.commits()[0].0.len(), 1);
}

#[tokio::test]
async fn one_atomic_commit_carries_provider_and_derived_sessions() {
    let first = equity_session(false);
    let second = CoverageSession {
        session_date: day(2024, 1, 22),
        open_time: instant(14, 30, 0, 0),
        close_time: instant(21, 0, 0, 0),
        is_partial: false,
    };
    // The provider serves only the first session; the second derives.
    let provider = FakeBarsProvider::new(move |_, _, _, _, _| {
        Ok(vec![bar(
            instant(14, 30, 0, 0),
            "AAPL",
            "189.9",
            "190.5",
            "189.9",
            "190.4",
            "30",
        )])
    });
    let reader = Arc::new(FakeTradeReader::default());
    reader.with_partition(
        AssetClass::Equity,
        "AAPL",
        second.session_date,
        stored_equity(
            "trade-rev-1",
            &[trade(at(2024, 1, 22, 14, 31), 1, "190.125", 100)],
        ),
    );
    let (transaction, _writer, committer) = transaction(provider, reader);

    let result = transaction
        .run(
            AssetClass::Equity,
            "AAPL",
            BarInterval::OneMinute,
            &[first, second],
        )
        .await
        .expect("run");

    assert_eq!(result.coverage.len(), 2);
    assert_eq!(result.coverage[0].bar_source, Some(BarSource::Provider));
    assert_eq!(result.coverage[1].bar_source, Some(BarSource::Derived));
    assert_eq!(result.objects.len(), 2);
    assert_eq!(committer.commit_count(), 1, "one commit per run");
    assert_eq!(committer.commits()[0].0.len(), 2);
    assert_eq!(committer.commits()[0].1.len(), 2);
}

#[tokio::test]
async fn partial_sessions_publish_partial_coverage() {
    let session = equity_session(true);
    let provider = FakeBarsProvider::returning(vec![bar(
        instant(14, 30, 0, 0),
        "AAPL",
        "189.9",
        "190.5",
        "189.9",
        "190.4",
        "30",
    )]);
    let reader = Arc::new(FakeTradeReader::default());
    let (transaction, _writer, _committer) = transaction(provider, reader);

    let result = transaction
        .run(
            AssetClass::Equity,
            "AAPL",
            BarInterval::OneMinute,
            &[session],
        )
        .await
        .expect("run");

    assert_eq!(result.coverage[0].status, CoverageStatus::Partial);
}

#[tokio::test]
async fn crypto_days_derive_with_fractional_volumes() {
    let session = crypto_day(day(2024, 1, 19), false);
    let provider = FakeBarsProvider::returning(Vec::new());
    let reader = Arc::new(FakeTradeReader::default());
    reader.with_partition(
        AssetClass::Crypto,
        "BTC/USD",
        session.session_date,
        StoredTradePartition {
            records: vec![
                StoredTrade::Crypto(lake_storage::CryptoTradeRecord {
                    timestamp: DateTime::from_micros(
                        DateTime::midnight(session.session_date).micros() + 30_000_000,
                    ),
                    symbol: "BTC/USD".to_string(),
                    trade_id: Some(1),
                    exchange: Some("CBSE".to_string()),
                    price: Decimal18::parse("41234.12").expect("price"),
                    size: Decimal18::parse("0.25").expect("size"),
                    taker_side: Some("B".to_string()),
                }),
                StoredTrade::Crypto(lake_storage::CryptoTradeRecord {
                    timestamp: DateTime::midnight(session.session_date),
                    symbol: "BTC/USD".to_string(),
                    trade_id: Some(2),
                    exchange: Some("CBSE".to_string()),
                    price: Decimal18::parse("41230.00").expect("price"),
                    size: Decimal18::parse("0.5").expect("size"),
                    taker_side: Some("S".to_string()),
                }),
            ],
            revision: "crypto-rev-1".to_string(),
        },
    );
    let (transaction, writer, _committer) = transaction(provider, reader);

    let result = transaction
        .run(
            AssetClass::Crypto,
            "btc/usd",
            BarInterval::OneDay,
            &[session],
        )
        .await
        .expect("run");

    assert_eq!(result.symbol, "BTC/USD");
    assert_eq!(result.coverage[0].bar_source, Some(BarSource::Derived));
    assert_eq!(
        result.coverage[0].source_revision.as_deref(),
        Some("crypto-rev-1")
    );
    let row = &writer.writes()[0].records[0];
    assert_eq!(row["open"], Value::String("41230".to_string()));
    assert_eq!(row["close"], Value::String("41234.12".to_string()));
    assert_eq!(row["volume"], Value::String("0.75".to_string()));
}
