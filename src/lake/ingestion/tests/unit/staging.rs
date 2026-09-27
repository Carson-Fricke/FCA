use super::*;
use lake_contracts::Date;

fn session_date() -> Date {
    Date::from_ymd(2024, 1, 19).expect("date")
}

fn trade_row(symbol: &str) -> Value {
    serde_json::json!({
        "timestamp": 1_705_674_600_000_000_000i64,
        "symbol": symbol,
        "trade_id": 1,
        "exchange": "XNAS",
        "price": "190.125",
        "size": 100,
        "conditions": ["@"],
        "tape": "C",
        "update": null,
    })
}

#[test]
fn write_stages_one_object_and_drain_returns_it_with_the_payload() {
    let writer = StagingPartitionWriter::new();
    let metadata = writer
        .write(
            &[trade_row("AAPL")],
            session_date(),
            AssetClass::Equity,
            DataType::Trade,
            "AAPL",
        )
        .expect("write");
    assert_eq!(
        metadata.path,
        "date=2024-01-19/asset_class=equity/data_type=trade/AAPL.parquet"
    );
    assert_eq!(metadata.rows, 1);

    let staged = writer.drain_staged();
    assert_eq!(staged.len(), 1);
    assert_eq!(staged[0].metadata, metadata);
    assert_eq!(staged[0].payload.len() as u64, metadata.bytes);
    assert!(staged[0].payload.starts_with(b"PAR1"));
    assert!(staged[0].payload.ends_with(b"PAR1"));

    assert!(writer.drain_staged().is_empty());
}

#[test]
fn reset_staged_discards_undrained_objects() {
    let writer = StagingPartitionWriter::new();
    writer
        .write(
            &[trade_row("AAPL")],
            session_date(),
            AssetClass::Equity,
            DataType::Trade,
            "AAPL",
        )
        .expect("write");
    writer.reset_staged();
    assert!(writer.drain_staged().is_empty());

    writer
        .write(
            &[trade_row("AAPL")],
            session_date(),
            AssetClass::Equity,
            DataType::Trade,
            "AAPL",
        )
        .expect("rewrite");
    assert_eq!(writer.drain_staged().len(), 1);
}

#[test]
fn write_surfaces_validation_errors_without_staging_anything() {
    let writer = StagingPartitionWriter::new();
    let invalid = serde_json::json!({
        "timestamp": 1_705_674_600_000_000_000i64,
        "symbol": "MSFT",
        "trade_id": 1,
        "exchange": "XNAS",
        "price": "190.125",
        "size": 100,
        "conditions": ["@"],
        "tape": "C",
    });
    let error = writer
        .write(
            &[invalid],
            session_date(),
            AssetClass::Equity,
            DataType::Trade,
            "AAPL",
        )
        .expect_err("partition symbol mismatch");
    assert!(error
        .to_string()
        .contains("does not match partition symbol"));
    assert!(writer.drain_staged().is_empty());
}
