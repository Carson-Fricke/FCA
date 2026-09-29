use super::*;
use crate::time::{Date, DateTime};
use num::Decimal18;
use std::str::FromStr;

fn dec(value: &str) -> Decimal18 {
    Decimal18::from_str(value).unwrap()
}

fn instant(seconds: i64) -> DateTime {
    DateTime::from_micros(seconds * 1_000_000)
}

fn trade(symbol: &str, seconds: i64, sequence: u64) -> MarketEvent {
    MarketEvent::Trade(TradeEvent::new(
        symbol.to_string(),
        instant(seconds),
        sequence,
        dec("10"),
        dec("1"),
    ))
}

fn session(day: i32) -> ExchangeSession {
    let date = Date::from_days(day);
    ExchangeSession {
        session_date: date,
        open_time: DateTime::midnight(date),
        close_time: DateTime::midnight(date.add_days(1)),
    }
}

#[test]
fn replay_merges_streams_in_order() {
    let stream = ReplayStream::new(
        "AAPL",
        Some(DataType::Trade),
        vec![trade("AAPL", 1, 1), trade("AAPL", 3, 3)],
    )
    .unwrap();
    let mut feed = LakeReplayFeed::new(vec![session(0)], vec![stream]).unwrap();
    let events = feed.events().unwrap();
    assert_eq!(events.len(), 2);
    assert_eq!(events[0].sequence(), 1);
    assert_eq!(events[1].sequence(), 3);
}

/// Golden fingerprints produced by `harness.data.replay` on the same events.
#[test]
fn replay_fingerprints_match_python_golden() {
    let day = Date::from_ymd(2024, 1, 2).unwrap();
    let session = ExchangeSession {
        session_date: day,
        open_time: DateTime::midnight(day),
        close_time: DateTime::midnight(day.add_days(1)),
    };
    let quote = QuoteEvent {
        event_id: "q-1".to_string(),
        timestamp: DateTime::from_ymd_hms(2024, 1, 2, 0, 0, 0, 0).unwrap(),
        sequence: 1,
        symbol: "AAPL".to_string(),
        bid_price: dec("100.000000000000000000"),
        bid_size: dec("1.000000000000000000"),
        ask_price: dec("101.000000000000000000"),
        ask_size: dec("2.000000000000000000"),
        venue: Some("V".to_string()),
    };
    let trade = TradeEvent {
        event_id: "t-1".to_string(),
        timestamp: DateTime::from_ymd_hms(2024, 1, 2, 0, 0, 1, 500_000).unwrap(),
        sequence: 2,
        symbol: "AAPL".to_string(),
        price: dec("100.500000000000000000"),
        quantity: dec("3.000000000000000000"),
        venue: Some("V".to_string()),
        trade_id: Some("42".to_string()),
    };
    let stream = ReplayStream::new(
        "AAPL",
        None,
        vec![MarketEvent::Quote(quote), MarketEvent::Trade(trade)],
    )
    .unwrap();
    let mut feed = LakeReplayFeed::new(vec![session], vec![stream]).unwrap();
    assert_eq!(
        feed.file_fingerprint(),
        "cc00a9100f6bb19947304e4398a16798bbb87e0569a0bbdfd8a90f25842f3fac"
    );
    assert_eq!(
        feed.fingerprints().data_fingerprint(),
        "85a05a272b90951dab3570794ffc08cdf42e02f17632e3aef658adea8fb0a3ba"
    );
    let events = feed.events().unwrap();
    assert_eq!(events.len(), 2);
    assert_eq!(
        feed.fingerprints().row_fingerprint(),
        "225bea5cab11acb50cad8c1533dd3783192520923c470f873de3c132a0fe5321"
    );
    assert_eq!(
        feed.fingerprints().data_fingerprint(),
        "71969743317ed52e99382036894c167d75c937613b2e803dbe9a2ae75b7f76e2"
    );
}

#[test]
fn event_frames_match_python_byte_layout() {
    let quote = QuoteEvent {
        event_id: "q-1".to_string(),
        timestamp: DateTime::from_ymd_hms(2024, 1, 2, 0, 0, 0, 0).unwrap(),
        sequence: 1,
        symbol: "AAPL".to_string(),
        bid_price: dec("100.000000000000000000"),
        bid_size: dec("1.000000000000000000"),
        ask_price: dec("101.000000000000000000"),
        ask_size: dec("2.000000000000000000"),
        venue: Some("V".to_string()),
    };
    let frame = event_fingerprint(&MarketEvent::Quote(quote));
    let expected = "000000000000000571756f74650000000000000003712d310000000000000020323032342d30312d30325430303a30303a30302e3030303030302b30303a303000000000000000013100000000000000044141504c00000000000000163130302e3030303030303030303030303030303030300000000000000014312e30303030303030303030303030303030303000000000000000163130312e3030303030303030303030303030303030300000000000000014322e303030303030303030303030303030303030000000000000000156";
    assert_eq!(hex_string(&frame), expected);
}

/// The route's Arrow IPC batches decode through the frozen provider columns:
/// integer `trade_id`/`size`, `exchange`/`bid_exchange` venues, and the
/// generated `{symbol}#{row}` event id when no `event_id` column exists.
#[test]
fn arrow_route_batches_decode_provider_columns() {
    use arrow_array::{
        ArrayRef, RecordBatch, StringArray, TimestampNanosecondArray, UInt32Array, UInt64Array,
    };
    use arrow_schema::{DataType as ArrowType, Schema, TimeUnit};
    use std::sync::Arc;

    fn price(values: Vec<i128>) -> ArrayRef {
        Arc::new(
            arrow_array::Decimal128Array::from(values.into_iter().map(Some).collect::<Vec<_>>())
                .with_precision_and_scale(38, 18)
                .unwrap(),
        )
    }

    let timestamp = ArrowType::Timestamp(TimeUnit::Nanosecond, Some("UTC".into()));
    let timestamp_ns = 1_704_204_000_000_000_000i64;

    let trade_schema = Arc::new(Schema::new(vec![
        arrow_schema::Field::new("timestamp", timestamp.clone(), false),
        arrow_schema::Field::new("symbol", ArrowType::Utf8, false),
        arrow_schema::Field::new("trade_id", ArrowType::UInt64, false),
        arrow_schema::Field::new("exchange", ArrowType::Utf8, false),
        arrow_schema::Field::new("price", ArrowType::Decimal128(38, 18), false),
        arrow_schema::Field::new("size", ArrowType::UInt32, false),
    ]));
    let trade_batch = RecordBatch::try_new(
        Arc::clone(&trade_schema),
        vec![
            Arc::new(
                TimestampNanosecondArray::from(vec![timestamp_ns]).with_timezone("UTC".to_string()),
            ),
            Arc::new(StringArray::from(vec!["SPY"])),
            Arc::new(UInt64Array::from(vec![7u64])),
            Arc::new(StringArray::from(vec!["XNAS"])),
            price(vec![450_010_000_000_000_000_000]),
            Arc::new(UInt32Array::from(vec![50u32])),
        ],
    )
    .unwrap();

    let trades = events_from_arrow(DataType::Trade, "SPY", &[trade_batch]).unwrap();
    assert_eq!(trades.len(), 1);
    match &trades[0] {
        MarketEvent::Trade(trade) => {
            assert_eq!(trade.event_id, "SPY#0");
            assert_eq!(trade.sequence, 7, "integer trade_id falls back as sequence");
            assert_eq!(trade.trade_id.as_deref(), Some("7"));
            assert_eq!(trade.venue.as_deref(), Some("XNAS"));
            assert_eq!(trade.quantity, dec("50"));
        }
        other => panic!("expected trade, got {other:?}"),
    }

    let quote_schema = Arc::new(Schema::new(vec![
        arrow_schema::Field::new("timestamp", timestamp, false),
        arrow_schema::Field::new("symbol", ArrowType::Utf8, false),
        arrow_schema::Field::new("bid_exchange", ArrowType::Utf8, false),
        arrow_schema::Field::new("bid_price", ArrowType::Decimal128(38, 18), false),
        arrow_schema::Field::new("bid_size", ArrowType::UInt32, false),
        arrow_schema::Field::new("ask_exchange", ArrowType::Utf8, false),
        arrow_schema::Field::new("ask_price", ArrowType::Decimal128(38, 18), false),
        arrow_schema::Field::new("ask_size", ArrowType::UInt32, false),
    ]));
    let quote_batch = RecordBatch::try_new(
        Arc::clone(&quote_schema),
        vec![
            Arc::new(
                TimestampNanosecondArray::from(vec![timestamp_ns]).with_timezone("UTC".to_string()),
            ),
            Arc::new(StringArray::from(vec!["SPY"])),
            Arc::new(StringArray::from(vec!["XNYS"])),
            price(vec![450_000_000_000_000_000_000]),
            Arc::new(UInt32Array::from(vec![100u32])),
            Arc::new(StringArray::from(vec!["XNYS"])),
            price(vec![450_020_000_000_000_000_000]),
            Arc::new(UInt32Array::from(vec![100u32])),
        ],
    )
    .unwrap();

    let quotes = events_from_arrow(DataType::Quote, "SPY", &[quote_batch]).unwrap();
    assert_eq!(quotes.len(), 1);
    match &quotes[0] {
        MarketEvent::Quote(quote) => {
            assert_eq!(quote.event_id, "SPY#0");
            assert_eq!(quote.sequence, 0, "quotes fall back to the row number");
            assert_eq!(quote.venue.as_deref(), Some("XNYS"));
            assert_eq!(quote.bid_size, dec("100"));
            assert_eq!(quote.ask_size, dec("100"));
        }
        other => panic!("expected quote, got {other:?}"),
    }
}

/// The frozen `equity.bars` schema decodes into exact, owned bar values.
#[test]
fn arrow_bars_batches_decode_exact_ohlcv() {
    use arrow_array::{ArrayRef, RecordBatch, StringArray, TimestampNanosecondArray};
    use arrow_schema::{DataType as ArrowType, Schema, TimeUnit};
    use std::sync::Arc;

    fn decimal(values: Vec<i128>) -> ArrayRef {
        Arc::new(
            arrow_array::Decimal128Array::from(values.into_iter().map(Some).collect::<Vec<_>>())
                .with_precision_and_scale(38, 18)
                .unwrap(),
        )
    }

    let timestamp = ArrowType::Timestamp(TimeUnit::Nanosecond, Some("UTC".into()));
    let timestamp_ns = 1_704_206_460_000_000_000i64;
    let schema = Arc::new(Schema::new(vec![
        arrow_schema::Field::new("timestamp", timestamp, false),
        arrow_schema::Field::new("symbol", ArrowType::Utf8, false),
        arrow_schema::Field::new("open", ArrowType::Decimal128(38, 18), false),
        arrow_schema::Field::new("high", ArrowType::Decimal128(38, 18), false),
        arrow_schema::Field::new("low", ArrowType::Decimal128(38, 18), false),
        arrow_schema::Field::new("close", ArrowType::Decimal128(38, 18), false),
        arrow_schema::Field::new("volume", ArrowType::Decimal128(38, 18), false),
    ]));
    let batch = RecordBatch::try_new(
        schema,
        vec![
            Arc::new(
                TimestampNanosecondArray::from(vec![timestamp_ns]).with_timezone("UTC".to_string()),
            ),
            Arc::new(StringArray::from(vec!["spy"])),
            decimal(vec![450_000_000_000_000_000_000]),
            decimal(vec![451_250_000_000_000_000_000]),
            decimal(vec![449_750_000_000_000_000_000]),
            decimal(vec![450_500_000_000_000_000_000]),
            decimal(vec![1_000_000_000_000_000_000_000]),
        ],
    )
    .unwrap();

    let bars = events_from_arrow(DataType::Bars, "SPY", &[batch]).unwrap();
    assert_eq!(bars.len(), 1);
    match &bars[0] {
        MarketEvent::Bar(bar) => {
            assert_eq!(bar.event_id, "SPY#0");
            assert_eq!(bar.sequence, 0);
            assert_eq!(bar.symbol, "SPY");
            assert_eq!(bar.timestamp, DateTime::from_nanos(timestamp_ns));
            assert_eq!(bar.open, dec("450.000000000000000000"));
            assert_eq!(bar.high, dec("451.250000000000000000"));
            assert_eq!(bar.low, dec("449.750000000000000000"));
            assert_eq!(bar.close, dec("450.500000000000000000"));
            assert_eq!(bar.volume, dec("1000.000000000000000000"));
            assert_eq!(bars[0].event_type(), EventType::Bar);
            assert_eq!(
                bars[0].ordering_key(),
                (timestamp_ns, 0, EventType::Bar.priority())
            );
        }
        other => panic!("expected bar, got {other:?}"),
    }
}
