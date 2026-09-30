//! Regression coverage for provider-bar session grouping.
//!
//! Provider minute bars are start-stamped, so the lake groups them on the
//! half-open session window `[open, close)`. The SIP row stamped exactly at the
//! close is the first extended-hours bar of the following period and must be
//! excluded instead of colliding with the final regular-session minute bucket
//! (RCA: QQQ 2025-05-30 falsification ensure failure).

use std::sync::{Arc, Mutex, MutexGuard};

use futures_util::future::BoxFuture;
use lake_catalog::{CoverageRecord, CoverageStatus};
use lake_contracts::{
    partition_path, AssetClass, BarInterval, DataError, DataType, Date, DateTime, Decimal18,
};
use lake_coverage::CoverageSession;
use lake_ingestion::{
    AtomicIngestCommitter, BarsDataProvider, BarsIngestTransaction, PartitionWriter,
    StagedParquetObject, StoredTradePartition, TradePartitionReader,
};
use lake_providers::{NormalizedBar, ProviderError};
use lake_storage::ParquetMetadata;
use serde_json::Value;

fn guard<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

fn instant(year: i32, month: u32, day_of_month: u32, hour: u32, minute: u32) -> DateTime {
    DateTime::from_ymd_hms(year, month, day_of_month, hour, minute, 0, 0).expect("instant")
}

fn session(year: i32, month: u32, day_of_month: u32) -> CoverageSession {
    CoverageSession {
        session_date: Date::from_ymd(year, month, day_of_month).expect("date"),
        open_time: instant(year, month, day_of_month, 13, 30),
        close_time: instant(year, month, day_of_month, 20, 0),
        is_partial: false,
    }
}

fn bar(timestamp: DateTime, close: &str) -> NormalizedBar {
    NormalizedBar {
        timestamp,
        symbol: "QQQ".to_string(),
        open: Decimal18::parse("519.11").expect("open"),
        high: Decimal18::parse("519.99").expect("high"),
        low: Decimal18::parse("519.00").expect("low"),
        close: Decimal18::parse(close).expect("close"),
        volume: Decimal18::parse("100").expect("volume"),
    }
}

struct FakeBarsProvider {
    bars: Vec<NormalizedBar>,
}

impl BarsDataProvider for FakeBarsProvider {
    fn fetch_bars<'a>(
        &'a self,
        _asset_class: AssetClass,
        _symbol: &'a str,
        _interval: BarInterval,
        _start: DateTime,
        _end: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedBar>, ProviderError>> {
        Box::pin(async move { Ok(self.bars.clone()) })
    }
}

struct EmptyTradeReader;

impl TradePartitionReader for EmptyTradeReader {
    fn read<'a>(
        &'a self,
        _asset_class: AssetClass,
        _symbol: &'a str,
        _session_date: Date,
    ) -> BoxFuture<'a, Result<Option<StoredTradePartition>, DataError>> {
        Box::pin(async move { Ok(None) })
    }
}

#[derive(Clone, Debug)]
struct WriteCall {
    records: Vec<Value>,
}

#[derive(Default)]
struct RecordingWriter {
    writes: Mutex<Vec<WriteCall>>,
    staged: Mutex<Vec<StagedParquetObject>>,
}

impl RecordingWriter {
    fn writes(&self) -> Vec<WriteCall> {
        guard(&self.writes).clone()
    }
}

impl PartitionWriter for RecordingWriter {
    fn write(
        &self,
        records: &[Value],
        session_date: Date,
        asset_class: AssetClass,
        data_type: DataType,
        interval: Option<&str>,
        symbol: &str,
    ) -> Result<ParquetMetadata, DataError> {
        let path = partition_path(session_date, asset_class, data_type, interval, symbol)?;
        let payload = serde_json::to_vec(records).map_err(DataError::io)?;
        let metadata = ParquetMetadata {
            path,
            rows: records.len() as u64,
            bytes: payload.len() as u64,
            checksum: "test-checksum".to_string(),
        };
        guard(&self.writes).push(WriteCall {
            records: records.to_vec(),
        });
        guard(&self.staged).push(StagedParquetObject {
            metadata: metadata.clone(),
            payload,
        });
        Ok(metadata)
    }

    fn drain_staged(&self) -> Vec<StagedParquetObject> {
        std::mem::take(&mut *guard(&self.staged))
    }

    fn reset_staged(&self) {
        guard(&self.staged).clear();
    }
}

#[derive(Default)]
struct RecordingCommitter {
    commits: Mutex<Vec<(Vec<StagedParquetObject>, Vec<CoverageRecord>)>>,
}

impl RecordingCommitter {
    fn commit_count(&self) -> usize {
        guard(&self.commits).len()
    }
}

impl AtomicIngestCommitter for RecordingCommitter {
    fn commit(
        &self,
        objects: &[StagedParquetObject],
        coverage: &[CoverageRecord],
    ) -> Result<(), DataError> {
        guard(&self.commits).push((objects.to_vec(), coverage.to_vec()));
        Ok(())
    }
}

fn transaction(
    bars: Vec<NormalizedBar>,
) -> (
    BarsIngestTransaction,
    Arc<RecordingWriter>,
    Arc<RecordingCommitter>,
) {
    let writer = Arc::new(RecordingWriter::default());
    let committer = Arc::new(RecordingCommitter::default());
    let provider = Arc::new(FakeBarsProvider { bars });
    let reader = Arc::new(EmptyTradeReader);
    let transaction =
        BarsIngestTransaction::new(provider, reader, writer.clone(), committer.clone());
    (transaction, writer, committer)
}

#[tokio::test]
async fn provider_bars_at_the_session_close_are_excluded() {
    let session = session(2025, 5, 30);
    let (transaction, writer, committer) = transaction(vec![
        bar(instant(2025, 5, 30, 8, 0), "519.00"),
        bar(instant(2025, 5, 30, 13, 30), "519.11"),
        bar(instant(2025, 5, 30, 19, 59), "519.13"),
        bar(instant(2025, 5, 30, 20, 0), "519.96"),
    ]);

    let result = transaction
        .run(
            AssetClass::Equity,
            "QQQ",
            BarInterval::OneMinute,
            &[session],
        )
        .await
        .expect("run");

    assert_eq!(result.coverage[0].status, CoverageStatus::Complete);
    let writes = writer.writes();
    assert_eq!(writes.len(), 1);
    let records = &writes[0].records;
    assert_eq!(records.len(), 2, "only regular-session minutes are kept");
    assert_eq!(
        records[0]["timestamp"].as_i64(),
        Some(instant(2025, 5, 30, 13, 30).to_nanos_i64())
    );
    assert_eq!(
        records[1]["timestamp"].as_i64(),
        Some(instant(2025, 5, 30, 19, 59).to_nanos_i64())
    );
    assert_eq!(records[1]["close"], Value::String("519.13".to_string()));
    assert_eq!(result.coverage[0].row_count, 2);
    assert_eq!(committer.commit_count(), 1);
}

#[tokio::test]
async fn repeated_provider_bar_timestamps_in_one_bucket_still_error() {
    let session = session(2025, 5, 30);
    let (transaction, writer, committer) = transaction(vec![
        bar(instant(2025, 5, 30, 15, 0), "519.11"),
        bar(instant(2025, 5, 30, 15, 0), "519.12"),
    ]);

    let result = transaction
        .run(
            AssetClass::Equity,
            "QQQ",
            BarInterval::OneMinute,
            &[session],
        )
        .await
        .expect("run");

    assert_eq!(result.coverage[0].status, CoverageStatus::Error);
    assert!(result.coverage[0]
        .error_message
        .as_deref()
        .expect("message")
        .contains("overlap a 1min bucket"));
    assert!(writer.writes().is_empty());
    assert_eq!(committer.commit_count(), 1);
}

#[tokio::test]
async fn each_session_in_a_union_window_drops_its_own_close_bar() {
    let sessions = [session(2025, 5, 30), session(2025, 6, 2)];
    let (transaction, writer, _committer) = transaction(vec![
        bar(instant(2025, 5, 30, 13, 30), "519.11"),
        bar(instant(2025, 5, 30, 19, 59), "519.13"),
        bar(instant(2025, 5, 30, 20, 0), "519.96"),
        bar(instant(2025, 5, 30, 20, 1), "519.97"),
        bar(instant(2025, 6, 2, 13, 30), "521.11"),
        bar(instant(2025, 6, 2, 19, 59), "521.13"),
        bar(instant(2025, 6, 2, 20, 0), "521.96"),
        bar(instant(2025, 6, 2, 20, 1), "521.97"),
    ]);

    let result = transaction
        .run(AssetClass::Equity, "QQQ", BarInterval::OneMinute, &sessions)
        .await
        .expect("run");

    assert_eq!(result.coverage.len(), 2);
    assert!(result
        .coverage
        .iter()
        .all(|record| record.status == CoverageStatus::Complete));
    let writes = writer.writes();
    assert_eq!(writes.len(), 2);
    assert!(writes.iter().all(|write| write.records.len() == 2));
    assert_eq!(
        writes[0].records[1]["timestamp"].as_i64(),
        Some(instant(2025, 5, 30, 19, 59).to_nanos_i64())
    );
    assert_eq!(
        writes[1].records[1]["timestamp"].as_i64(),
        Some(instant(2025, 6, 2, 19, 59).to_nanos_i64())
    );
}
