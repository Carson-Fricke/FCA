use super::*;
use crate::clock::ManualClock;
use crate::events::{BarEvent, TradeEvent};
use std::str::FromStr;

#[test]
fn live_part_path_matches_layout() {
    let date = Date::from_ymd(2024, 1, 5).unwrap();
    assert_eq!(
        live_part_path(
            "prod",
            date,
            AssetClass::Equity,
            DataType::Trade,
            None,
            "AAPL",
            3
        )
        .unwrap(),
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
    let events = crate::replay::events_from_parquet(DataType::Trade, "AAPL", &payload).unwrap();
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

#[test]
fn recorder_writes_bars_with_the_frozen_schema_and_interval_partition() {
    let root = std::env::temp_dir().join(format!(
        "fca-live-recorder-bars-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|value| value.as_nanos())
            .unwrap_or(0)
    ));
    let store = std::rc::Rc::new(LocalLiveObjectStore::new(&root).unwrap());
    let clock: std::rc::Rc<dyn Clock> =
        std::rc::Rc::new(ManualClock::new(DateTime::from_micros(0)));
    let config = LiveRecorderConfig::new("prod", AssetClass::Equity, store.clone(), clock, 2)
        .unwrap()
        .with_bar_interval("1min");
    let mut recorder = LiveRecorder::new(config);
    recorder
        .record(&MarketEvent::Bar(BarEvent {
            event_id: "bar-1".to_string(),
            timestamp: DateTime::from_ymd_hms(2024, 1, 5, 15, 30, 0, 0).unwrap(),
            symbol: "AAPL".to_string(),
            sequence: 7,
            open: Decimal18::from_str("10.5").unwrap(),
            high: Decimal18::from_str("11").unwrap(),
            low: Decimal18::from_str("10").unwrap(),
            close: Decimal18::from_str("10.75").unwrap(),
            volume: Decimal18::from_str("12345").unwrap(),
        }))
        .unwrap();
    recorder.close().unwrap();
    assert_eq!(recorder.row_counts().get("bars/AAPL").copied(), Some(1));

    let payload = store
        .read(
            "live/prod/date=2024-01-05/asset_class=equity/data_type=bars/interval=1min/\
             AAPL.part-000000.parquet",
        )
        .unwrap();
    assert_eq!(&payload[..4], b"PAR1");
    let events = crate::replay::events_from_parquet(DataType::Bars, "AAPL", &payload).unwrap();
    assert_eq!(events.len(), 1);
    match &events[0] {
        MarketEvent::Bar(bar) => {
            // The frozen bars schema carries no event_id column, so decode
            // generates `{symbol}#{row}` exactly like quote and trade parts.
            assert_eq!(bar.event_id, "AAPL#0");
            assert_eq!(bar.sequence, 0);
            assert_eq!(bar.open, Decimal18::from_str("10.5").unwrap());
            assert_eq!(bar.high, Decimal18::from_str("11").unwrap());
            assert_eq!(bar.low, Decimal18::from_str("10").unwrap());
            assert_eq!(bar.close, Decimal18::from_str("10.75").unwrap());
            assert_eq!(bar.volume, Decimal18::from_str("12345").unwrap());
        }
        other => panic!("expected bar, got {other:?}"),
    }
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn recording_bars_without_an_interval_fails_loud() {
    let store = std::rc::Rc::new(
        LocalLiveObjectStore::new(std::env::temp_dir().join("fca-live-recorder-no-interval"))
            .unwrap(),
    );
    let clock: std::rc::Rc<dyn Clock> =
        std::rc::Rc::new(ManualClock::new(DateTime::from_micros(0)));
    let config = LiveRecorderConfig::new("prod", AssetClass::Equity, store, clock, 2).unwrap();
    let mut recorder = LiveRecorder::new(config);
    let error = recorder
        .record(&MarketEvent::Bar(BarEvent {
            event_id: "bar-1".to_string(),
            timestamp: DateTime::from_ymd_hms(2024, 1, 5, 15, 30, 0, 0).unwrap(),
            symbol: "AAPL".to_string(),
            sequence: 1,
            open: Decimal18::ZERO,
            high: Decimal18::ZERO,
            low: Decimal18::ZERO,
            close: Decimal18::ZERO,
            volume: Decimal18::ZERO,
        }))
        .expect_err("bars without an interval must fail loud");
    assert!(error.to_string().contains("bar interval"), "{error}");
}
