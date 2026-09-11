use super::*;
use crate::time::DateTime;
use num::Decimal18;

fn dec(value: &str) -> Decimal18 {
    value.parse().unwrap()
}

fn quote(sequence: u64, micros: i64, bid: &str, ask: &str) -> MarketEvent {
    MarketEvent::Quote(QuoteEvent {
        event_id: format!("q-{sequence}"),
        timestamp: DateTime::from_micros(micros),
        sequence,
        symbol: "SPY".to_string(),
        bid_price: dec(bid),
        ask_price: dec(ask),
        bid_size: dec("100"),
        ask_size: dec("100"),
        venue: Some("XNYS".to_string()),
    })
}

fn trade(sequence: u64, micros: i64, price: &str) -> MarketEvent {
    MarketEvent::Trade(TradeEvent {
        event_id: format!("t-{sequence}"),
        timestamp: DateTime::from_micros(micros),
        sequence,
        symbol: "SPY".to_string(),
        price: dec(price),
        quantity: dec("25"),
        venue: Some("XNYS".to_string()),
        trade_id: Some(format!("{}", sequence + 40)),
    })
}

fn temp_dir(name: &str) -> PathBuf {
    let path = std::env::temp_dir().join(format!(
        "harness-lake-reader-{}-{name}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|value| value.as_nanos())
            .unwrap_or(0)
    ));
    std::fs::create_dir_all(&path).expect("temp dir");
    path
}

#[test]
fn writes_then_reads_quote_and_trade_partitions() {
    let root = temp_dir("roundtrip");
    let date = Date::from_ymd(2024, 1, 2).unwrap();
    let quotes = vec![
        quote(0, 1_000_000, "450.00", "450.02"),
        quote(1, 2_000_000, "450.01", "450.03"),
    ];
    let trades = vec![trade(2, 1_500_000, "450.01")];
    let quote_path = write_lake_partition(
        &root,
        date,
        AssetClass::Equity,
        DataType::Quote,
        "SPY",
        &quotes,
    )
    .expect("write quotes");
    let trade_path = write_lake_partition(
        &root,
        date,
        AssetClass::Equity,
        DataType::Trade,
        "SPY",
        &trades,
    )
    .expect("write trades");
    assert!(quote_path.ends_with("date=2024-01-02/asset_class=equity/data_type=quote/SPY.parquet"));
    assert!(trade_path.ends_with("date=2024-01-02/asset_class=equity/data_type=trade/SPY.parquet"));

    let reader = LocalLakeReader::new(&root);
    let events = reader
        .read(AssetClass::Equity, date, date, &["SPY".to_string()])
        .expect("read");
    assert_eq!(events.len(), 3);
    // Replay order: the 1.5s trade sorts between the 1.0s and 2.0s quotes.
    assert_eq!(events[0].sequence(), 0);
    assert_eq!(events[1].sequence(), 2);
    assert_eq!(events[2].sequence(), 1);
    match &events[1] {
        MarketEvent::Trade(TradeEvent {
            price,
            quantity,
            trade_id,
            ..
        }) => {
            assert_eq!(price.to_string(), "450.010000000000000000");
            assert_eq!(quantity.to_string(), "25.000000000000000000");
            assert_eq!(trade_id.as_deref(), Some("42"));
        }
        other => panic!("expected trade, got {other:?}"),
    }
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn missing_partitions_read_as_empty() {
    let root = temp_dir("missing");
    let date = Date::from_ymd(2024, 1, 2).unwrap();
    let reader = LocalLakeReader::new(&root);
    let events = reader
        .read(AssetClass::Equity, date, date, &["SPY".to_string()])
        .expect("read");
    assert!(events.is_empty());
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn rejects_inverted_ranges() {
    let reader = LocalLakeReader::new("unused");
    let start = Date::from_ymd(2024, 1, 3).unwrap();
    let end = Date::from_ymd(2024, 1, 2).unwrap();
    assert!(reader
        .read(AssetClass::Equity, start, end, &["SPY".to_string()])
        .is_err());
}
