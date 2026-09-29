//! End-to-end file-query behavior over the in-memory catalog.
//!
//! Pins the Python file-mode contract: only complete partitions with canonical
//! object paths become files, every other entry is incomplete (`empty` and
//! `delisted` included), `ensure` drives the injected callback exactly once,
//! and files require a configured URL issuer.

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use lake_catalog::{
    CatalogRepository, CoverageRecord, CoverageStatus, DelistingType, ListingRecord, ListingType,
    MemoryCatalogRepository, MemoryListingRepository,
};
use lake_contracts::{partition_path, AssetClass, DataError, DataType, Date, DateTime};
use lake_query::{
    execute_file_query, EnsureCallback, FileQueryContext, FileQueryRequest, FileUrlIssuer,
    QueryApplicationError,
};
use serde_json::json;

fn date(month: u32, day: u32) -> Date {
    Date::from_ymd(2024, month, day).expect("valid date")
}

fn as_of_monday() -> DateTime {
    DateTime::from_ymd_hms(2024, 1, 22, 12, 0, 0, 0).expect("valid datetime")
}

fn equity_request(symbol: &str, start_date: Date, end_date: Date) -> FileQueryRequest {
    FileQueryRequest {
        asset_class: AssetClass::Equity,
        data_type: DataType::Trade,
        symbol: Some(symbol.to_string()),
        start_date,
        end_date,
        ensure: false,
        interval: None,
    }
}

fn crypto_request(symbol: &str, start_date: Date, end_date: Date) -> FileQueryRequest {
    FileQueryRequest {
        asset_class: AssetClass::Crypto,
        data_type: DataType::Quote,
        symbol: Some(symbol.to_string()),
        start_date,
        end_date,
        ensure: false,
        interval: None,
    }
}

fn coverage_record(
    asset_class: AssetClass,
    data_type: DataType,
    symbol: &str,
    session_date: Date,
    status: CoverageStatus,
    object_path: Option<String>,
) -> CoverageRecord {
    let complete = status == CoverageStatus::Complete;
    CoverageRecord::new(
        asset_class,
        data_type,
        symbol,
        session_date,
        status,
        object_path,
        if complete { 12 } else { 0 },
        if complete { 3456 } else { 0 },
        if complete {
            Some("checksum".to_string())
        } else {
            None
        },
        None,
        None,
    )
    .expect("valid coverage record")
}

fn complete_equity_record(symbol: &str, session_date: Date) -> CoverageRecord {
    let object_path = partition_path(
        session_date,
        AssetClass::Equity,
        DataType::Trade,
        None,
        symbol,
    )
    .expect("canonical path");
    coverage_record(
        AssetClass::Equity,
        DataType::Trade,
        symbol,
        session_date,
        CoverageStatus::Complete,
        Some(object_path),
    )
}

fn complete_crypto_quote_record(session_date: Date) -> CoverageRecord {
    let object_path = partition_path(
        session_date,
        AssetClass::Crypto,
        DataType::Quote,
        None,
        "BTC/USD",
    )
    .expect("canonical path");
    coverage_record(
        AssetClass::Crypto,
        DataType::Quote,
        "BTC/USD",
        session_date,
        CoverageStatus::Complete,
        Some(object_path),
    )
}

fn empty_record(symbol: &str, session_date: Date) -> CoverageRecord {
    coverage_record(
        AssetClass::Equity,
        DataType::Trade,
        symbol,
        session_date,
        CoverageStatus::Empty,
        None,
    )
}

fn partial_record(symbol: &str, session_date: Date) -> CoverageRecord {
    coverage_record(
        AssetClass::Equity,
        DataType::Trade,
        symbol,
        session_date,
        CoverageStatus::Partial,
        None,
    )
}

/// A recording SAS issuer returning deterministic Azure-like URLs.
#[derive(Default)]
struct RecordingIssuer {
    paths: Mutex<Vec<String>>,
}

impl RecordingIssuer {
    fn paths(&self) -> Vec<String> {
        self.paths.lock().expect("lock").clone()
    }
}

impl FileUrlIssuer for RecordingIssuer {
    fn issue(&self, object_path: &str) -> Result<String, DataError> {
        self.paths
            .lock()
            .expect("lock")
            .push(object_path.to_string());
        Ok(format!(
            "https://blob.test/{object_path}?read-only-expiring"
        ))
    }
}

/// An issuer that always fails, standing in for storage outages.
struct FailingIssuer;

impl FileUrlIssuer for FailingIssuer {
    fn issue(&self, _object_path: &str) -> Result<String, DataError> {
        Err(DataError::io("issuer unavailable"))
    }
}

fn counting_callback(calls: Arc<AtomicUsize>) -> EnsureCallback {
    Arc::new(move |_, _, _, _, _, _| {
        let calls = Arc::clone(&calls);
        Box::pin(async move {
            calls.fetch_add(1, Ordering::SeqCst);
            Ok(())
        })
    })
}

fn failing_callback(calls: Arc<AtomicUsize>) -> EnsureCallback {
    Arc::new(move |_, _, _, _, _, _| {
        let calls = Arc::clone(&calls);
        Box::pin(async move {
            calls.fetch_add(1, Ordering::SeqCst);
            Err(QueryApplicationError::new(
                "ensure_source",
                "the launcher exploded",
                None,
            ))
        })
    })
}

fn ensuring_callback(
    catalog: Arc<MemoryCatalogRepository>,
    records: Vec<CoverageRecord>,
    calls: Arc<AtomicUsize>,
) -> EnsureCallback {
    Arc::new(move |_, _, _, _, _, _| {
        let catalog = Arc::clone(&catalog);
        let records = records.clone();
        let calls = Arc::clone(&calls);
        Box::pin(async move {
            calls.fetch_add(1, Ordering::SeqCst);
            for record in records {
                catalog.upsert(&record).map_err(|error| {
                    QueryApplicationError::new("ensure_failed", error.to_string(), None)
                })?;
            }
            Ok(())
        })
    })
}

#[tokio::test]
async fn complete_coverage_returns_canonical_files_and_issued_urls() {
    let jan_18 = date(1, 18);
    let catalog = MemoryCatalogRepository::from_records([complete_equity_record("AAPL", jan_18)]);
    let issuer = RecordingIssuer::default();
    let request = equity_request("aapl", jan_18, jan_18);

    let response = execute_file_query(
        FileQueryContext {
            catalog: &catalog,
            listings: None,
            sas_issuer: Some(&issuer),
            ensure_callback: None,
            bounded_query: None,
            force_authorized: false,
            as_of: as_of_monday(),
        },
        &request,
    )
    .await
    .expect("complete query");

    let expected_path =
        partition_path(jan_18, AssetClass::Equity, DataType::Trade, None, "AAPL").expect("path");
    assert_eq!(
        response.results,
        lake_query::FileQueryResults {
            requested_sessions: 1,
            returned_files: 1,
            complete_sessions: 1,
            pending_sessions: 0,
            state: "complete".to_string(),
        }
    );
    assert_eq!(
        response.limitations,
        vec![
            "Only complete catalog partitions are returned.".to_string(),
            "URLs are read-only, object-scoped, and expire according to SAS policy.".to_string(),
        ]
    );
    assert_eq!(response.files.len(), 1);
    let file = &response.files[0];
    assert_eq!(file.asset_class, AssetClass::Equity);
    assert_eq!(file.data_type, DataType::Trade);
    assert_eq!(file.symbol, "AAPL");
    assert_eq!(file.session_date, jan_18);
    assert_eq!(file.object_path, expected_path);
    assert_eq!(
        file.url,
        format!("https://blob.test/{expected_path}?read-only-expiring")
    );
    assert_eq!(file.sas_url.as_deref(), Some(file.url.as_str()));
    assert_eq!(file.row_count, 12);
    assert_eq!(file.byte_size, 3456);
    assert_eq!(file.checksum.as_deref(), Some("checksum"));
    assert_eq!(issuer.paths(), vec![expected_path]);
}

#[tokio::test]
async fn complete_and_empty_window_is_incomplete_without_ensure() {
    let catalog = MemoryCatalogRepository::from_records([
        complete_equity_record("AAPL", date(1, 18)),
        empty_record("AAPL", date(1, 19)),
    ]);
    let issuer = RecordingIssuer::default();
    let request = equity_request("aapl", date(1, 18), date(1, 19));

    let error = execute_file_query(
        FileQueryContext {
            catalog: &catalog,
            listings: None,
            sas_issuer: Some(&issuer),
            ensure_callback: None,
            bounded_query: None,
            force_authorized: false,
            as_of: as_of_monday(),
        },
        &request,
    )
    .await
    .expect_err("empty sessions are not complete partitions");

    assert_eq!(error.code, "coverage_incomplete");
    assert_eq!(
        error.message,
        "requested coverage is incomplete; set ensure=true to request coverage"
    );
    assert_eq!(
        error.details,
        Some(json!({
            "sessions": [{"session_date": "2024-01-19", "state": "empty"}],
        }))
    );
    assert!(issuer.paths().is_empty());
}

#[tokio::test]
async fn ensure_requested_with_an_empty_session_stays_pending() {
    let catalog = MemoryCatalogRepository::from_records([
        complete_equity_record("AAPL", date(1, 18)),
        empty_record("AAPL", date(1, 19)),
    ]);
    let issuer = RecordingIssuer::default();
    let calls = Arc::new(AtomicUsize::new(0));
    let mut request = equity_request("aapl", date(1, 18), date(1, 19));
    request.ensure = true;

    let response = execute_file_query(
        FileQueryContext {
            catalog: &catalog,
            listings: None,
            sas_issuer: Some(&issuer),
            ensure_callback: Some(counting_callback(Arc::clone(&calls))),
            bounded_query: None,
            force_authorized: false,
            as_of: as_of_monday(),
        },
        &request,
    )
    .await
    .expect("ensure query");

    assert_eq!(calls.load(Ordering::SeqCst), 1);
    assert_eq!(response.results.requested_sessions, 2);
    assert_eq!(response.results.returned_files, 1);
    assert_eq!(response.results.complete_sessions, 1);
    assert_eq!(response.results.pending_sessions, 1);
    assert_eq!(response.results.state, "ensure_pending");
    assert_eq!(response.files.len(), 1);
    assert_eq!(response.files[0].session_date, date(1, 18));
    assert_eq!(
        response.limitations,
        vec![
            "Only complete catalog partitions are returned.".to_string(),
            "URLs are read-only, object-scoped, and expire according to SAS policy.".to_string(),
            "Coverage ensure was requested but is not complete yet.".to_string(),
        ]
    );
    assert_eq!(
        issuer.paths(),
        vec![partition_path(
            date(1, 18),
            AssetClass::Equity,
            DataType::Trade,
            None,
            "AAPL"
        )
        .expect("path")]
    );
}

#[tokio::test]
async fn closed_partial_without_ensure_is_incomplete_with_pending_details() {
    let catalog = MemoryCatalogRepository::from_records([partial_record("AAPL", date(1, 18))]);
    let issuer = RecordingIssuer::default();
    let request = equity_request("aapl", date(1, 18), date(1, 18));

    let error = execute_file_query(
        FileQueryContext {
            catalog: &catalog,
            listings: None,
            sas_issuer: Some(&issuer),
            ensure_callback: None,
            bounded_query: None,
            force_authorized: false,
            as_of: as_of_monday(),
        },
        &request,
    )
    .await
    .expect_err("partial is not complete");

    assert_eq!(error.code, "coverage_incomplete");
    assert_eq!(
        error.details,
        Some(json!({
            "sessions": [{"session_date": "2024-01-18", "state": "partial"}],
        }))
    );
    assert!(issuer.paths().is_empty());
}

#[tokio::test]
async fn ensure_invokes_the_callback_once_and_reports_pending_when_still_incomplete() {
    let catalog = MemoryCatalogRepository::from_records([partial_record("AAPL", date(1, 18))]);
    let calls = Arc::new(AtomicUsize::new(0));
    let mut request = equity_request("aapl", date(1, 18), date(1, 19));
    request.ensure = true;

    let response = execute_file_query(
        FileQueryContext {
            catalog: &catalog,
            listings: None,
            sas_issuer: None,
            ensure_callback: Some(counting_callback(Arc::clone(&calls))),
            bounded_query: None,
            force_authorized: false,
            as_of: as_of_monday(),
        },
        &request,
    )
    .await
    .expect("ensure query");

    assert_eq!(calls.load(Ordering::SeqCst), 1);
    assert!(response.files.is_empty());
    assert_eq!(response.results.requested_sessions, 2);
    assert_eq!(response.results.returned_files, 0);
    assert_eq!(response.results.complete_sessions, 0);
    assert_eq!(response.results.pending_sessions, 2);
    assert_eq!(response.results.state, "ensure_pending");
    assert_eq!(response.limitations.len(), 3);
}

#[tokio::test]
async fn satisfied_after_ensure_returns_complete() {
    let catalog = Arc::new(MemoryCatalogRepository::new());
    let issuer = RecordingIssuer::default();
    let calls = Arc::new(AtomicUsize::new(0));
    let callback = ensuring_callback(
        Arc::clone(&catalog),
        vec![
            complete_equity_record("AAPL", date(1, 18)),
            complete_equity_record("AAPL", date(1, 19)),
        ],
        Arc::clone(&calls),
    );
    let mut request = equity_request("aapl", date(1, 18), date(1, 19));
    request.ensure = true;

    let response = execute_file_query(
        FileQueryContext {
            catalog: catalog.as_ref(),
            listings: None,
            sas_issuer: Some(&issuer),
            ensure_callback: Some(callback),
            bounded_query: None,
            force_authorized: false,
            as_of: as_of_monday(),
        },
        &request,
    )
    .await
    .expect("ensure query");

    assert_eq!(calls.load(Ordering::SeqCst), 1);
    assert_eq!(response.results.state, "complete");
    assert_eq!(response.results.returned_files, 2);
    assert_eq!(response.results.complete_sessions, 2);
    assert_eq!(response.results.pending_sessions, 0);
    assert_eq!(response.limitations.len(), 2);
    assert_eq!(issuer.paths().len(), 2);
}

/// Recorded ensure-callback arguments.
type CapturedCalls = Arc<Mutex<Vec<(AssetClass, DataType, Option<String>, String, Date, Date)>>>;

#[tokio::test]
async fn ensure_callback_receives_the_request_identity() {
    let catalog = MemoryCatalogRepository::new();
    let captured: CapturedCalls = Arc::new(Mutex::new(Vec::new()));
    let callback: EnsureCallback = Arc::new({
        let captured = Arc::clone(&captured);
        move |asset_class, data_type, interval, symbol, start_date, end_date| {
            let captured = Arc::clone(&captured);
            Box::pin(async move {
                captured.lock().expect("lock").push((
                    asset_class,
                    data_type,
                    interval,
                    symbol,
                    start_date,
                    end_date,
                ));
                Ok(())
            })
        }
    });
    let mut request = equity_request("aapl", date(1, 18), date(1, 19));
    request.ensure = true;

    execute_file_query(
        FileQueryContext {
            catalog: &catalog,
            listings: None,
            sas_issuer: None,
            ensure_callback: Some(callback),
            bounded_query: None,
            force_authorized: false,
            as_of: as_of_monday(),
        },
        &request,
    )
    .await
    .expect("ensure query");

    assert_eq!(
        captured.lock().expect("lock").as_slice(),
        [(
            AssetClass::Equity,
            DataType::Trade,
            None,
            "aapl".to_string(),
            date(1, 18),
            date(1, 19)
        )]
    );
}

#[tokio::test]
async fn ensure_failure_maps_to_ensure_failed() {
    let catalog = MemoryCatalogRepository::new();
    let calls = Arc::new(AtomicUsize::new(0));
    let mut request = equity_request("aapl", date(1, 18), date(1, 19));
    request.ensure = true;

    let error = execute_file_query(
        FileQueryContext {
            catalog: &catalog,
            listings: None,
            sas_issuer: None,
            ensure_callback: Some(failing_callback(Arc::clone(&calls))),
            bounded_query: None,
            force_authorized: false,
            as_of: as_of_monday(),
        },
        &request,
    )
    .await
    .expect_err("ensure failure");

    assert_eq!(calls.load(Ordering::SeqCst), 1);
    assert_eq!(error.code, "ensure_failed");
    assert_eq!(error.message, "coverage ensure failed");
    assert_eq!(
        error.details,
        Some(json!({"error": "the launcher exploded"}))
    );
}

#[tokio::test]
async fn ensure_without_a_callback_is_ensure_unavailable() {
    let catalog = MemoryCatalogRepository::new();
    let mut request = equity_request("aapl", date(1, 18), date(1, 19));
    request.ensure = true;

    let error = execute_file_query(
        FileQueryContext {
            catalog: &catalog,
            listings: None,
            sas_issuer: None,
            ensure_callback: None,
            bounded_query: None,
            force_authorized: false,
            as_of: as_of_monday(),
        },
        &request,
    )
    .await
    .expect_err("no callback");

    assert_eq!(error.code, "ensure_unavailable");
    assert_eq!(error.message, "coverage ensure is not configured");
}

#[tokio::test]
async fn satisfied_coverage_never_invokes_the_callback() {
    let catalog =
        MemoryCatalogRepository::from_records([complete_equity_record("AAPL", date(1, 18))]);
    let issuer = RecordingIssuer::default();
    let calls = Arc::new(AtomicUsize::new(0));
    let mut request = equity_request("aapl", date(1, 18), date(1, 18));
    request.ensure = true;

    let response = execute_file_query(
        FileQueryContext {
            catalog: &catalog,
            listings: None,
            sas_issuer: Some(&issuer),
            ensure_callback: Some(counting_callback(Arc::clone(&calls))),
            bounded_query: None,
            force_authorized: false,
            as_of: as_of_monday(),
        },
        &request,
    )
    .await
    .expect("query");

    assert_eq!(calls.load(Ordering::SeqCst), 0);
    assert_eq!(response.results.state, "complete");
    assert_eq!(response.files.len(), 1);
}

#[tokio::test]
async fn malformed_catalog_path_is_incomplete() {
    let malformed = coverage_record(
        AssetClass::Equity,
        DataType::Trade,
        "AAPL",
        date(1, 18),
        CoverageStatus::Complete,
        Some("../outside.parquet".to_string()),
    );
    let catalog = MemoryCatalogRepository::from_records([malformed]);
    let issuer = RecordingIssuer::default();
    let request = equity_request("aapl", date(1, 18), date(1, 18));

    let error = execute_file_query(
        FileQueryContext {
            catalog: &catalog,
            listings: None,
            sas_issuer: Some(&issuer),
            ensure_callback: None,
            bounded_query: None,
            force_authorized: false,
            as_of: as_of_monday(),
        },
        &request,
    )
    .await
    .expect_err("non-canonical path");

    assert_eq!(error.code, "coverage_incomplete");
    assert_eq!(
        error.details,
        Some(json!({
            "sessions": [{"session_date": "2024-01-18", "state": "complete"}],
        }))
    );
    assert!(issuer.paths().is_empty());
}

#[tokio::test]
async fn missing_issuer_with_files_is_sas_unavailable() {
    let catalog =
        MemoryCatalogRepository::from_records([complete_equity_record("AAPL", date(1, 18))]);
    let request = equity_request("aapl", date(1, 18), date(1, 18));

    let error = execute_file_query(
        FileQueryContext {
            catalog: &catalog,
            listings: None,
            sas_issuer: None,
            ensure_callback: None,
            bounded_query: None,
            force_authorized: false,
            as_of: as_of_monday(),
        },
        &request,
    )
    .await
    .expect_err("no issuer");

    assert_eq!(error.code, "sas_unavailable");
    assert_eq!(error.message, "file query SAS issuance is not configured");
}

#[tokio::test]
async fn issuer_failure_maps_to_sas_unavailable() {
    let catalog =
        MemoryCatalogRepository::from_records([complete_equity_record("AAPL", date(1, 18))]);
    let request = equity_request("aapl", date(1, 18), date(1, 18));

    let error = execute_file_query(
        FileQueryContext {
            catalog: &catalog,
            listings: None,
            sas_issuer: Some(&FailingIssuer),
            ensure_callback: None,
            bounded_query: None,
            force_authorized: false,
            as_of: as_of_monday(),
        },
        &request,
    )
    .await
    .expect_err("issuer failure");

    assert_eq!(error.code, "sas_unavailable");
    assert!(error.message.contains("file query SAS issuance failed"));
}

#[tokio::test]
async fn symbol_is_required_for_equity_and_crypto() {
    let catalog =
        MemoryCatalogRepository::from_records([complete_equity_record("AAPL", date(1, 18))]);
    let issuer = RecordingIssuer::default();
    let mut request = equity_request("aapl", date(1, 18), date(1, 18));
    request.symbol = None;

    let error = execute_file_query(
        FileQueryContext {
            catalog: &catalog,
            listings: None,
            sas_issuer: Some(&issuer),
            ensure_callback: None,
            bounded_query: None,
            force_authorized: false,
            as_of: as_of_monday(),
        },
        &request,
    )
    .await
    .expect_err("symbol required");

    assert_eq!(error.code, "validation_error");
    assert_eq!(
        error.message,
        "symbol is required for equity and crypto queries"
    );
    assert!(issuer.paths().is_empty());
}

#[tokio::test]
async fn non_equity_crypto_asset_classes_are_validation_errors() {
    let catalog = MemoryCatalogRepository::new();
    let issuer = RecordingIssuer::default();
    let mut request = equity_request("SPY240119C00500000", date(1, 19), date(1, 19));
    request.asset_class = AssetClass::Option;

    let error = execute_file_query(
        FileQueryContext {
            catalog: &catalog,
            listings: None,
            sas_issuer: Some(&issuer),
            ensure_callback: None,
            bounded_query: None,
            force_authorized: false,
            as_of: as_of_monday(),
        },
        &request,
    )
    .await
    .expect_err("option selection is out of scope");

    assert_eq!(error.code, "validation_error");
    assert_eq!(error.message, "file queries support equity and crypto only");
}

#[tokio::test]
async fn crypto_weekend_window_uses_canonical_pairs() {
    let session_dates = [date(1, 19), date(1, 20), date(1, 21)];
    let catalog =
        MemoryCatalogRepository::from_records(session_dates.map(complete_crypto_quote_record));
    let issuer = RecordingIssuer::default();
    let request = crypto_request("btc/usd", date(1, 19), date(1, 21));

    let response = execute_file_query(
        FileQueryContext {
            catalog: &catalog,
            listings: None,
            sas_issuer: Some(&issuer),
            ensure_callback: None,
            bounded_query: None,
            force_authorized: false,
            as_of: as_of_monday(),
        },
        &request,
    )
    .await
    .expect("crypto query");

    let expected_paths: Vec<String> = session_dates
        .iter()
        .map(|session_date| {
            partition_path(
                *session_date,
                AssetClass::Crypto,
                DataType::Quote,
                None,
                "BTC/USD",
            )
            .expect("path")
        })
        .collect();
    assert_eq!(response.results.requested_sessions, 3);
    assert_eq!(response.results.returned_files, 3);
    assert_eq!(response.results.state, "complete");
    assert_eq!(
        response
            .files
            .iter()
            .map(|file| file.object_path.clone())
            .collect::<Vec<_>>(),
        expected_paths
    );
    assert!(expected_paths
        .iter()
        .all(|path| path.ends_with("/BTC%2FUSD.parquet")));
    assert!(response.files.iter().all(|file| file.symbol == "BTC/USD"));
    assert_eq!(issuer.paths(), expected_paths);
}

fn complete_equity_bars_record(interval: &str, session_date: Date) -> CoverageRecord {
    let object_path = partition_path(
        session_date,
        AssetClass::Equity,
        DataType::Bars,
        Some(interval),
        "AAPL",
    )
    .expect("canonical path");
    CoverageRecord::new(
        AssetClass::Equity,
        DataType::Bars,
        "AAPL",
        session_date,
        CoverageStatus::Complete,
        Some(object_path),
        12,
        3456,
        Some("checksum".to_string()),
        None,
        Some(interval.to_string()),
    )
    .expect("valid bars record")
}

#[tokio::test]
async fn bars_queries_return_the_interval_partition_and_reject_bad_intervals() {
    let jan_18 = date(1, 18);
    let catalog =
        MemoryCatalogRepository::from_records([complete_equity_bars_record("1min", jan_18)]);
    let issuer = RecordingIssuer::default();
    let request = FileQueryRequest {
        data_type: DataType::Bars,
        interval: Some("1min".to_string()),
        ..equity_request("aapl", jan_18, jan_18)
    };

    let response = execute_file_query(
        FileQueryContext {
            catalog: &catalog,
            listings: None,
            sas_issuer: Some(&issuer),
            ensure_callback: None,
            bounded_query: None,
            force_authorized: false,
            as_of: as_of_monday(),
        },
        &request,
    )
    .await
    .expect("bars query");

    assert_eq!(response.files.len(), 1);
    assert_eq!(response.files[0].data_type, DataType::Bars);
    assert_eq!(response.files[0].interval.as_deref(), Some("1min"));
    assert_eq!(
        response.files[0].object_path,
        "date=2024-01-18/asset_class=equity/data_type=bars/interval=1min/AAPL.parquet"
    );

    let day = FileQueryRequest {
        interval: Some("1d".to_string()),
        ..request.clone()
    };
    let error = execute_file_query(
        FileQueryContext {
            catalog: &catalog,
            listings: None,
            sas_issuer: Some(&issuer),
            ensure_callback: None,
            bounded_query: None,
            force_authorized: false,
            as_of: as_of_monday(),
        },
        &day,
    )
    .await
    .expect_err("1d is a different partition");
    assert_eq!(error.code, "coverage_incomplete");

    let missing = FileQueryRequest {
        interval: None,
        ..request.clone()
    };
    let error = execute_file_query(
        FileQueryContext {
            catalog: &catalog,
            listings: None,
            sas_issuer: Some(&issuer),
            ensure_callback: None,
            bounded_query: None,
            force_authorized: false,
            as_of: as_of_monday(),
        },
        &missing,
    )
    .await
    .expect_err("bars without an interval");
    assert_eq!(error.code, "validation_error");
    assert_eq!(error.message, "bars requires an interval");

    let unknown = FileQueryRequest {
        interval: Some("5m".to_string()),
        ..request
    };
    let error = execute_file_query(
        FileQueryContext {
            catalog: &catalog,
            listings: None,
            sas_issuer: Some(&issuer),
            ensure_callback: None,
            bounded_query: None,
            force_authorized: false,
            as_of: as_of_monday(),
        },
        &unknown,
    )
    .await
    .expect_err("unknown interval token");
    assert_eq!(error.code, "validation_error");
    assert_eq!(
        error.message,
        "invalid bar interval: \"5m\"; expected one of 1min, 1d"
    );
}

#[tokio::test]
async fn delisted_sessions_are_incomplete_without_ensure() {
    let listings = MemoryListingRepository::from_records([ListingRecord::new(
        "AAPL",
        "AAPL",
        date(1, 15),
        Some(date(1, 16)),
        ListingType::Ipo,
        Some(DelistingType::Merger),
        None,
    )
    .expect("valid listing")]);
    let catalog = MemoryCatalogRepository::new();
    let issuer = RecordingIssuer::default();
    let request = equity_request("aapl", date(1, 18), date(1, 18));

    let error = execute_file_query(
        FileQueryContext {
            catalog: &catalog,
            listings: Some(&listings),
            sas_issuer: Some(&issuer),
            ensure_callback: None,
            bounded_query: None,
            force_authorized: false,
            as_of: as_of_monday(),
        },
        &request,
    )
    .await
    .expect_err("delisted is terminal but not a complete partition");

    assert_eq!(error.code, "coverage_incomplete");
    assert_eq!(
        error.details,
        Some(json!({
            "sessions": [{"session_date": "2024-01-18", "state": "delisted"}],
        }))
    );
    assert!(issuer.paths().is_empty());
}
