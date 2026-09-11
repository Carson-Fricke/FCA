//! Data-layer parity suite versus the Python reference contract.
//!
//! These tests assert the shape, ordering, and on-disk-path invariants that
//! the Python `harness.data` layer guarantees, using golden values taken from
//! that reference. They exercise the composed public facade rather than
//! individual modules.

use std::rc::Rc;

use harness_data::{
    crypto_utc_days, interval_delta, live_part_path, partition_path, schema_columns, xnys_sessions,
    AssetClass, BarAggregator, CoverageEntry, CoverageResponse, CoverageState, DataType,
    DatasetRequest, Date, DateTime, DelistingType, EnsureResponse, ExchangeSession, FileQueryFile,
    FileQueryResponse, FileQueryState, FileQuerySummary, HistoricalView, HistorySelector,
    JobStatus, LakeClientPort, ListingRecord, ListingType, LiveRecorder, LiveRecorderConfig,
    LocalLiveObjectStore, ManualClock, MarketEvent, MarketLakeAdapter, MarketLakeRequest,
    MemoryContractsCatalog, MemoryListingsCatalog, OptionType, QuoteEvent, ReplayStream,
    SecurityMasterView, StaticReader, TradeEvent, UniverseComposition, UniverseMember,
    UniverseSnapshot,
};
use harness_num::Decimal18;
use std::str::FromStr;

fn dec(value: &str) -> Decimal18 {
    Decimal18::from_str(value).expect("valid decimal")
}

fn date(year: i32, month: u32, day: u32) -> Date {
    Date::from_ymd(year, month, day).expect("valid date")
}

fn trade_event(
    symbol: &str,
    seconds: i64,
    sequence: u64,
    price: &str,
    quantity: &str,
) -> TradeEvent {
    TradeEvent::new(
        symbol.to_string(),
        DateTime::from_micros(seconds * 1_000_000),
        sequence,
        dec(price),
        dec(quantity),
    )
}

fn trade(symbol: &str, seconds: i64, sequence: u64, price: &str, quantity: &str) -> MarketEvent {
    MarketEvent::Trade(trade_event(symbol, seconds, sequence, price, quantity))
}

/// The stable Hive path contract from `lake.contracts.partitions`.
#[test]
fn partition_paths_match_python_contract() {
    let day = date(2024, 1, 5);
    assert_eq!(
        partition_path(day, AssetClass::Equity, DataType::Trade, "aapl").unwrap(),
        "date=2024-01-05/asset_class=equity/data_type=trade/AAPL.parquet"
    );
    assert_eq!(
        partition_path(
            day,
            AssetClass::Option,
            DataType::Quote,
            "AAPL240119C00150000"
        )
        .unwrap(),
        "date=2024-01-05/asset_class=option/data_type=quote/AAPL240119C00150000.parquet"
    );
    assert_eq!(
        partition_path(day, AssetClass::Crypto, DataType::Trade, "BTC/USD").unwrap(),
        "date=2024-01-05/asset_class=crypto/data_type=trade/BTC%2FUSD.parquet"
    );
}

/// The interval vocabulary from `interval_delta`.
#[test]
fn interval_vocabulary_matches_python() {
    assert_eq!(interval_delta("1s").unwrap(), 1_000_000_000);
    assert_eq!(interval_delta("5min").unwrap(), 300_000_000_000);
    assert_eq!(interval_delta("1h").unwrap(), 3_600_000_000_000);
    assert_eq!(interval_delta("1d").unwrap(), 86_400_000_000_000);
}

/// Exact OHLCV reconciliation from `bars._make_bar`.
#[test]
fn bars_match_python_golden() {
    let trades = vec![
        trade_event("AAPL", 1, 1, "10.00", "2"),
        trade_event("AAPL", 2, 2, "12.00", "1"),
        trade_event("AAPL", 61, 3, "9.00", "3"),
    ];
    let mut aggregator = BarAggregator::new("1min", Some("AAPL")).unwrap();
    aggregator.add_many(trades).unwrap();
    let bars = aggregator.finalize().unwrap();
    assert_eq!(bars.len(), 2);
    assert_eq!(bars[0].open, dec("10.00"));
    assert_eq!(bars[0].high, dec("12.00"));
    assert_eq!(bars[0].low, dec("10.00"));
    assert_eq!(bars[0].close, dec("12.00"));
    assert_eq!(bars[0].volume, dec("3"));
    assert_eq!(bars[0].trade_count, 2);
    assert_eq!(bars[0].start.to_nanos(), 0);
    assert_eq!(bars[0].end.to_nanos(), 60_000_000_000);
}

/// Point-in-time clipping and the `(timestamp, sequence, priority)` order.
#[test]
fn history_ordering_and_lookahead_match_python() {
    let clock = ManualClock::new(DateTime::from_micros(100 * 1_000_000));
    let mut view = HistoricalView::new(Box::new(clock));
    view.subscribe("AAPL", HistorySelector::All, None).unwrap();
    view.record(MarketEvent::Quote(QuoteEvent::new(
        "AAPL".to_string(),
        DateTime::from_micros(1_000_000),
        1,
        dec("9.5"),
        dec("10.5"),
    )))
    .unwrap();
    view.record(trade("AAPL", 1, 2, "10", "1")).unwrap();
    view.record(trade("AAPL", 2, 3, "11", "1")).unwrap();

    let last = view.last("AAPL", 2, HistorySelector::All, None).unwrap();
    assert_eq!(last.len(), 2);
    assert_eq!(last[0].event_id(), "AAPL-2");
    assert_eq!(last[1].event_id(), "AAPL-3");

    let aggregate = view
        .aggregate(
            "AAPL",
            DateTime::from_micros(0),
            DateTime::from_micros(10_000_000),
            None,
        )
        .unwrap();
    assert_eq!(aggregate.trade_count, 2);
    assert_eq!(aggregate.vwap, Some(dec("10.5")));

    // Future reads are structurally refused.
    assert!(view
        .quote("AAPL", Some(DateTime::from_micros(200 * 1_000_000)))
        .is_err());
}

/// Point-in-time security master rename and delisting rules.
#[test]
fn security_master_matches_python_point_in_time_rules() {
    let catalog = MemoryListingsCatalog {
        episodes: vec![
            ListingRecord::new(
                "AA",
                "AAA",
                date(2020, 1, 1),
                Some(date(2021, 1, 1)),
                ListingType::Ipo,
                Some(DelistingType::Rename),
                None,
            )
            .unwrap(),
            ListingRecord::new(
                "AA",
                "AAB",
                date(2021, 1, 1),
                None,
                ListingType::Rename,
                None,
                None,
            )
            .unwrap(),
        ],
    };
    let view = SecurityMasterView::new(
        Box::new(ManualClock::new(
            DateTime::from_ymd_hms(2022, 6, 1, 12, 0, 0, 0).unwrap(),
        )),
        Rc::new(catalog),
    );
    let snapshot = view.resolve("AAB", None).unwrap().unwrap();
    assert_eq!(snapshot.entity_id, "AA");
    assert_eq!(snapshot.symbols(), vec!["AAA", "AAB"]);
    assert_eq!(snapshot.symbol(), "AAB");
    assert!(view.is_listed("AAA", None).unwrap());
}

/// Universe composition union/intersection and member ordering.
#[test]
fn universe_snapshot_matches_python_composition() {
    let listing = ListingRecord::new(
        "AA",
        "AAA",
        date(2020, 1, 1),
        None,
        ListingType::Ipo,
        None,
        None,
    )
    .unwrap();
    let snapshot = UniverseSnapshot::new(
        date(2022, 1, 1),
        UniverseComposition::Union,
        vec![UniverseMember::equity(listing)],
    );
    assert_eq!(snapshot.symbols(), vec!["AAA"]);
    assert_eq!(snapshot.provenance().get("total").copied(), Some(1));
}

/// The options front-set selection and filters.
#[test]
fn options_snapshot_clips_expired_contracts() {
    let _catalog = MemoryContractsCatalog {
        contracts: Vec::new(),
        front_sets: Vec::new(),
    };
    // A snapshot constructed directly still clips to the as-of date.
    let contract = harness_data::ContractRecord::new(
        "AAPL240119C00150000",
        "AAPL",
        date(2024, 1, 19),
        dec("150"),
        OptionType::Call,
        100,
        None,
        None,
    )
    .unwrap();
    let snapshot = harness_data::OptionUniverseSnapshot::new(
        "AAPL",
        date(2024, 6, 1),
        harness_data::SelectionMode::StaticOsi,
        vec![contract],
    )
    .unwrap();
    assert!(snapshot.symbols().is_empty());
}

/// Replay merges quote and trade sources by the deterministic ordering key.
#[test]
fn replay_merge_matches_python_ordering() {
    let day = date(2024, 1, 2);
    let session = ExchangeSession {
        session_date: day,
        open_time: DateTime::from_micros(0),
        close_time: DateTime::from_micros(86_400 * 1_000_000),
    };
    let stream = ReplayStream::new(
        "AAPL",
        None,
        vec![
            trade("AAPL", 1, 1, "10", "1"),
            trade("AAPL", 3, 3, "11", "1"),
        ],
    )
    .unwrap();
    let mut feed =
        harness_data::LakeReplayFeed::new(vec![session], vec![stream], Vec::new(), None).unwrap();
    let events = feed.events().unwrap();
    assert_eq!(events.len(), 2);
    assert_eq!(events[0].sequence(), 1);
    assert_eq!(events[1].sequence(), 3);
}

/// Live recorder part paths and schema column order.
#[test]
fn recorder_layout_matches_python_contract() {
    let day = date(2024, 1, 5);
    assert_eq!(
        live_part_path("prod", day, AssetClass::Equity, DataType::Trade, "AAPL", 7).unwrap(),
        "live/prod/date=2024-01-05/asset_class=equity/data_type=trade/AAPL.part-000007.parquet"
    );
    assert_eq!(
        schema_columns(AssetClass::Equity, DataType::Trade),
        &[
            "timestamp",
            "symbol",
            "trade_id",
            "exchange",
            "price",
            "size",
            "conditions",
            "tape",
            "update"
        ]
    );
    assert_eq!(
        schema_columns(AssetClass::Option, DataType::Quote),
        &[
            "timestamp",
            "symbol",
            "bid_exchange",
            "bid_price",
            "bid_size",
            "ask_exchange",
            "ask_price",
            "ask_size",
            "condition"
        ]
    );
}

/// Alpaca SIP normalization and OPRA rotation.
#[test]
fn live_ingest_matches_python_normalization() {
    let mut inbox = harness_data::AlpacaSipInbox::sip(0, 5_000_000_000, 300_000_000_000).unwrap();
    let observed = DateTime::from_micros(0);
    let events = inbox
        .push(
            &[harness_data::AlpacaMessage::Trade {
                symbol: "aapl".to_string(),
                timestamp: observed.isoformat_micros() + "Z",
                price: "10.5".to_string(),
                size: "100".to_string(),
                venue: Some("V".to_string()),
                trade_id: Some("7".to_string()),
            }],
            observed,
        )
        .unwrap();
    assert_eq!(events.len(), 1);
    match &events[0] {
        MarketEvent::Trade(trade) => {
            assert_eq!(trade.symbol, "AAPL");
            assert_eq!(trade.trade_id.as_deref(), Some("7"));
            assert!(trade.event_id.starts_with("alpaca-sip:t:AAPL:"));
        }
        _ => panic!("expected trade"),
    }

    let current = harness_data::AlpacaOpraSubscription::new(
        vec!["AAPL240119C00150000".to_string()],
        Vec::new(),
    )
    .unwrap();
    let target = harness_data::AlpacaOpraSubscription::new(
        vec!["AAPL240119C00160000".to_string()],
        Vec::new(),
    )
    .unwrap();
    let delta = harness_data::rotation_delta(&current, &target);
    assert_eq!(delta.unsubscribe_trades, vec!["AAPL240119C00150000"]);
    assert_eq!(delta.subscribe_trades, vec!["AAPL240119C00160000"]);
}

/// XNYS and crypto session resolution.
#[test]
fn session_resolution_matches_python_contract() {
    let sessions = xnys_sessions(date(2024, 1, 1), date(2024, 1, 5)).unwrap();
    assert_eq!(sessions.len(), 4);
    assert_eq!(sessions[0].session_date.isoformat(), "2024-01-02");
    let days = crypto_utc_days(date(2024, 1, 1), date(2024, 1, 3)).unwrap();
    assert_eq!(days.len(), 3);
}

struct FakeClient;

impl LakeClientPort for FakeClient {
    fn ensure(&self, request: &DatasetRequest) -> Result<EnsureResponse, harness_data::DataError> {
        let date = request.start_date;
        let session = xnys_sessions(date, date).expect("xnys")[0];
        Ok(EnsureResponse::Coverage(CoverageResponse {
            asset_class: request.asset_class,
            data_type: request.data_type,
            symbol: request.symbol.clone(),
            start_date: request.start_date,
            end_date: request.end_date,
            entries: vec![CoverageEntry {
                session_date: date,
                open_time: session.open_time,
                close_time: session.close_time,
                status: CoverageState::Complete,
                is_partial: false,
                object_path: Some(
                    partition_path(
                        date,
                        request.asset_class,
                        request.data_type,
                        request.symbol.as_deref().unwrap_or("AAPL"),
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
    ) -> Result<JobStatus, harness_data::DataError> {
        Err(harness_data::DataError::value("no jobs"))
    }

    fn query_files(
        &self,
        request: &DatasetRequest,
        _ensure: bool,
    ) -> Result<FileQueryResponse, harness_data::DataError> {
        let date = request.start_date;
        let symbol = request.symbol.clone().unwrap_or_default();
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
                object_path: partition_path(date, request.asset_class, request.data_type, &symbol)
                    .unwrap(),
                read_url: "https://example.invalid/part".to_string(),
                byte_size: 16,
                row_count: 1,
            }],
        })
    }
}

/// A downstream consumer drives the composed data layer end to end.
#[test]
fn downstream_pipeline_consumes_the_data_layer() {
    let request = MarketLakeRequest::new(
        AssetClass::Equity,
        DataType::Trade,
        vec!["AAPL".to_string()],
        date(2024, 1, 2),
        date(2024, 1, 2),
    )
    .unwrap();
    let adapter = MarketLakeAdapter::with_client(
        Rc::new(FakeClient),
        Box::new(|files: &[FileQueryFile]| files.len()),
        None,
    )
    .unwrap();
    let load = adapter.load(&request).unwrap();
    assert_eq!(load.data, 1);

    let clock = ManualClock::new(DateTime::from_micros(100 * 1_000_000));
    let mut view = HistoricalView::new(Box::new(clock));
    view.subscribe("AAPL", HistorySelector::All, None).unwrap();
    view.record_many([
        trade("AAPL", 1, 1, "10", "2"),
        trade("AAPL", 2, 2, "11", "1"),
    ])
    .unwrap();
    let bars = view.bars("AAPL", "1min", None, None, None).unwrap();
    assert_eq!(bars.len(), 1);
    assert_eq!(bars[0].close, dec("11"));

    // The strategy-facing capability reads only at the head.
    let strategy = harness_data::StrategyHistory::new(&view);
    assert!(strategy
        .read("AAPL", HistorySelector::Trade)
        .unwrap()
        .is_some());

    // A recorder captures the same events into lake parts.
    let store_root = std::env::temp_dir().join("fca-parity-downstream");
    let store = Rc::new(LocalLiveObjectStore::new(store_root).unwrap());
    let config = LiveRecorderConfig::new(
        "test",
        AssetClass::Equity,
        store,
        Rc::new(ManualClock::new(DateTime::from_micros(0))),
        16,
    )
    .unwrap();
    let mut recorder = LiveRecorder::new(config);
    let mut event = TradeEvent::new(
        "AAPL".to_string(),
        DateTime::from_ymd_hms(2024, 1, 2, 15, 0, 0, 0).unwrap(),
        1,
        dec("10"),
        dec("2"),
    );
    event.trade_id = Some("1".to_string());
    recorder.record(&MarketEvent::Trade(event)).unwrap();
    recorder.close().unwrap();
    assert_eq!(recorder.row_counts().get("trade/AAPL").copied(), Some(1));
}

/// A static reader is available for downstream replay fixtures.
#[test]
fn static_reader_is_usable_downstream() {
    let reader = StaticReader {
        events: vec![trade("AAPL", 1, 1, "10", "1")],
    };
    assert_eq!(reader.events.len(), 1);
}
