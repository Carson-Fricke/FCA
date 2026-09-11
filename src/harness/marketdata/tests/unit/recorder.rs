use super::*;
use crate::clock::ManualClock;
use crate::events::TradeEvent;
use std::str::FromStr;

#[test]
fn live_part_path_matches_layout() {
    let date = Date::from_ymd(2024, 1, 5).unwrap();
    assert_eq!(
        live_part_path("prod", date, AssetClass::Equity, DataType::Trade, "AAPL", 3).unwrap(),
        "live/prod/date=2024-01-05/asset_class=equity/data_type=trade/AAPL.part-000003.parquet"
    );
}

#[test]
fn recorder_writes_rows_in_schema_order() {
    let store = std::rc::Rc::new(
        LocalLiveObjectStore::new(std::env::temp_dir().join("fca-live-recorder-test")).unwrap(),
    );
    let clock: std::rc::Rc<dyn Clock> =
        std::rc::Rc::new(ManualClock::new(DateTime::from_micros(0)));
    let config =
        LiveRecorderConfig::new("prod", AssetClass::Equity, store.clone(), clock, 2).unwrap();
    let mut recorder = LiveRecorder::new(config);
    let mut event = TradeEvent::new(
        "AAPL".to_string(),
        DateTime::from_ymd_hms(2024, 1, 5, 15, 30, 0, 0).unwrap(),
        1,
        Decimal18::from_str("10.5").unwrap(),
        Decimal18::from_str("100").unwrap(),
    );
    event.trade_id = Some("1".to_string());
    recorder.record(&MarketEvent::Trade(event)).unwrap();
    recorder.close().unwrap();
    assert!(recorder.closed());
    assert_eq!(recorder.row_counts().get("trade/AAPL").copied(), Some(1));
    let payload = store
        .read(
            "live/prod/date=2024-01-05/asset_class=equity/data_type=trade/AAPL.part-000000.parquet",
        )
        .unwrap();
    assert_eq!(&payload[..4], b"PAR1");
    assert_eq!(&payload[payload.len() - 4..], b"PAR1");
    let file = crate::lake::FileQueryFile {
        asset_class: AssetClass::Equity,
        data_type: DataType::Trade,
        symbol: "AAPL".to_string(),
        session_date: Date::from_ymd(2024, 1, 5).unwrap(),
        object_path: "date=2024-01-05/asset_class=equity/data_type=trade/AAPL.parquet".to_string(),
        read_url: "file://local".to_string(),
        byte_size: payload.len() as u64,
        row_count: 1,
    };
    let events = crate::replay::events_from_parquet(&file, &payload).unwrap();
    assert_eq!(events.len(), 1);
    match &events[0] {
        MarketEvent::Trade(trade) => {
            assert_eq!(trade.symbol, "AAPL");
            assert_eq!(trade.price, Decimal18::from_str("10.5").unwrap());
            assert_eq!(trade.quantity, Decimal18::from_str("100").unwrap());
            assert_eq!(trade.trade_id.as_deref(), Some("1"));
        }
        _ => panic!("expected trade"),
    }
}
