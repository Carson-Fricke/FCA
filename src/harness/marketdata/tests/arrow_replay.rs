//! Strict Arrow decoding and direct caller mapping at the public data boundary.
use arrow_array::{
    ArrayRef, Decimal128Array, Float64Array, RecordBatch, StringArray, TimestampNanosecondArray,
    UInt64Array,
};
use marketdata::{events_from_arrow, map_arrow_events, DataType, MarketEvent};
use std::sync::Arc;

fn trades() -> RecordBatch {
    RecordBatch::try_from_iter(vec![
        (
            "timestamp",
            Arc::new(TimestampNanosecondArray::from(vec![
                1_234_567_890,
                1_234_567_891,
            ])) as ArrayRef,
        ),
        (
            "symbol",
            Arc::new(StringArray::from(vec![Some(" msft "), None])) as ArrayRef,
        ),
        (
            "sequence",
            Arc::new(UInt64Array::from(vec![7, 8])) as ArrayRef,
        ),
        (
            "trade_id",
            Arc::new(UInt64Array::from(vec![42, 43])) as ArrayRef,
        ),
        (
            "price",
            Arc::new(
                Decimal128Array::from(vec![40001, 40002])
                    .with_precision_and_scale(10, 2)
                    .unwrap(),
            ) as ArrayRef,
        ),
        ("size", Arc::new(UInt64Array::from(vec![2, 3])) as ArrayRef),
        (
            "exchange",
            Arc::new(StringArray::from(vec!["NASDAQ", "NYSE"])) as ArrayRef,
        ),
    ])
    .unwrap()
}

#[test]
fn direct_mapping_preserves_exact_values_aliases_and_row_identity_across_batches() {
    let batch = trades();
    let mapped = map_arrow_events(DataType::Trade, "MSFT", &[batch.clone(), batch], |event| {
        let MarketEvent::Trade(t) = event else {
            panic!("trade");
        };
        (
            t.event_id,
            t.symbol,
            t.sequence,
            t.price.to_string(),
            t.quantity.to_string(),
            t.venue,
            t.trade_id,
        )
    })
    .unwrap();
    assert_eq!(mapped.len(), 4);
    assert_eq!(
        mapped[0],
        (
            "MSFT#0".into(),
            "MSFT".into(),
            7,
            "400.010000000000000000".into(),
            "2.000000000000000000".into(),
            Some("NASDAQ".into()),
            Some("42".into()),
        )
    );
    assert_eq!(mapped[3].0, "MSFT#3");
    assert_eq!(mapped[3].1, "MSFT");
    assert_eq!(mapped[3].3, "400.020000000000000000");
}

#[test]
fn typed_decoding_rejects_wrong_numeric_types_and_missing_required_values() {
    let batch = RecordBatch::try_from_iter(vec![
        (
            "timestamp",
            Arc::new(TimestampNanosecondArray::from(vec![1])) as ArrayRef,
        ),
        (
            "price",
            Arc::new(Float64Array::from(vec![400.0])) as ArrayRef,
        ),
        ("size", Arc::new(UInt64Array::from(vec![1])) as ArrayRef),
    ])
    .unwrap();
    assert!(events_from_arrow(DataType::Trade, "MSFT", &[batch])
        .unwrap_err()
        .to_string()
        .contains("decimal128 or integer"));
    let null = RecordBatch::try_from_iter(vec![
        (
            "timestamp",
            Arc::new(TimestampNanosecondArray::from(vec![1])) as ArrayRef,
        ),
        (
            "price",
            Arc::new(Float64Array::from(vec![None::<f64>])) as ArrayRef,
        ),
    ])
    .unwrap();
    assert!(events_from_arrow(DataType::Trade, "MSFT", &[null])
        .unwrap_err()
        .to_string()
        .contains("missing price"));
}

#[test]
fn null_optional_columns_keep_their_fallback_semantics() {
    let batch = RecordBatch::try_from_iter(vec![
        (
            "timestamp",
            Arc::new(TimestampNanosecondArray::from(vec![1])) as ArrayRef,
        ),
        (
            "symbol",
            Arc::new(Float64Array::from(vec![None::<f64>])) as ArrayRef,
        ),
        ("price", Arc::new(UInt64Array::from(vec![400])) as ArrayRef),
        ("quantity", Arc::new(UInt64Array::from(vec![1])) as ArrayRef),
    ])
    .unwrap();
    let decoded = events_from_arrow(DataType::Trade, "MSFT", &[batch]).unwrap();
    let MarketEvent::Trade(t) = &decoded[0] else {
        panic!("trade");
    };
    assert_eq!(t.symbol, "MSFT");
    assert_eq!(t.sequence, 0);
    assert_eq!(t.event_id, "MSFT#0");
}
