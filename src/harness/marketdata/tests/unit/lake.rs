use super::*;
use std::cell::Cell;

#[test]
fn partition_paths_are_hive_and_encoded() {
    let date = Date::from_ymd(2024, 1, 5).unwrap();
    assert_eq!(
        partition_path(date, AssetClass::Equity, DataType::Trade, "aapl").unwrap(),
        "date=2024-01-05/asset_class=equity/data_type=trade/AAPL.parquet"
    );
    assert_eq!(
        partition_path(date, AssetClass::Crypto, DataType::Quote, "BTC/USD").unwrap(),
        "date=2024-01-05/asset_class=crypto/data_type=quote/BTC%2FUSD.parquet"
    );
}

#[test]
fn crypto_sessions_cover_every_day() {
    let start = Date::from_ymd(2024, 1, 1).unwrap();
    let end = Date::from_ymd(2024, 1, 3).unwrap();
    let days = crypto_utc_days(start, end).unwrap();
    assert_eq!(days.len(), 3);
}

#[test]
fn xnys_skips_weekends_and_holidays() {
    // 2024-01-01 is New Year's Day (Monday); the first session is Jan 2.
    let start = Date::from_ymd(2024, 1, 1).unwrap();
    let end = Date::from_ymd(2024, 1, 5).unwrap();
    let sessions = xnys_sessions(start, end).unwrap();
    assert_eq!(sessions.len(), 4);
    assert_eq!(sessions[0].session_date.isoformat(), "2024-01-02");
    // July 4 2024 is a Thursday holiday.
    let start = Date::from_ymd(2024, 7, 4).unwrap();
    let end = Date::from_ymd(2024, 7, 4).unwrap();
    assert!(xnys_sessions(start, end).unwrap().is_empty());
}

struct FakeClient {
    calls: Cell<usize>,
}

impl LakeClientPort for FakeClient {
    fn ensure(&self, _request: &DatasetRequest) -> Result<EnsureResponse, DataError> {
        self.calls.set(self.calls.get() + 1);
        Ok(EnsureResponse::Coverage(CoverageResponse {
            asset_class: AssetClass::Equity,
            data_type: DataType::Trade,
            symbol: Some("AAPL".to_string()),
            start_date: Date::from_ymd(2024, 1, 2).unwrap(),
            end_date: Date::from_ymd(2024, 1, 2).unwrap(),
            entries: vec![CoverageEntry {
                session_date: Date::from_ymd(2024, 1, 2).unwrap(),
                open_time: xnys_sessions(
                    Date::from_ymd(2024, 1, 2).unwrap(),
                    Date::from_ymd(2024, 1, 2).unwrap(),
                )
                .unwrap()[0]
                    .open_time,
                close_time: xnys_sessions(
                    Date::from_ymd(2024, 1, 2).unwrap(),
                    Date::from_ymd(2024, 1, 2).unwrap(),
                )
                .unwrap()[0]
                    .close_time,
                status: CoverageState::Complete,
                is_partial: false,
                object_path: Some(
                    partition_path(
                        Date::from_ymd(2024, 1, 2).unwrap(),
                        AssetClass::Equity,
                        DataType::Trade,
                        "AAPL",
                    )
                    .unwrap(),
                ),
            }],
        }))
    }

    fn wait_for_job(
        &self,
        _job_id: &str,
        _timeout: f64,
        _poll_interval: f64,
    ) -> Result<JobStatus, DataError> {
        unimplemented!()
    }

    fn query_files(
        &self,
        request: &DatasetRequest,
        _ensure: bool,
    ) -> Result<FileQueryResponse, DataError> {
        let date = Date::from_ymd(2024, 1, 2).unwrap();
        let symbol = request.symbol.clone().unwrap();
        Ok(FileQueryResponse {
            results: FileQuerySummary {
                state: FileQueryState::Complete,
                pending_sessions: 0,
                requested_sessions: 1,
                complete_sessions: 1,
                returned_files: 1,
            },
            files: vec![FileQueryFile {
                asset_class: request.asset_class,
                data_type: request.data_type,
                symbol: symbol.clone(),
                session_date: date,
                object_path: expected_path(request, date).unwrap(),
                read_url: "https://example.invalid/blob".to_string(),
                byte_size: 10,
                row_count: 3,
            }],
        })
    }
}

#[test]
fn adapter_ensures_then_reads_sorted_files() {
    let client = Rc::new(FakeClient {
        calls: Cell::new(0),
    });
    let adapter = MarketLakeAdapter::with_client(
        client,
        Box::new(|files: &[FileQueryFile]| files.len()),
        Some(DEFAULT_EGRESS_WARNING_THRESHOLD_BYTES),
    )
    .unwrap();
    let request = MarketLakeRequest::new(
        AssetClass::Equity,
        DataType::Trade,
        vec!["AAPL".to_string()],
        Date::from_ymd(2024, 1, 2).unwrap(),
        Date::from_ymd(2024, 1, 2).unwrap(),
    )
    .unwrap();
    let load = adapter.load(&request).unwrap();
    assert_eq!(load.data, 1);
    assert_eq!(load.projected_bytes, 10);
    assert_eq!(load.sessions.len(), 1);
}
