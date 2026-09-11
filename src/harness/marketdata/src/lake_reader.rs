//! Local Parquet/Arrow market-lake reader.
//!
//! Reads Hive-partitioned `date=/asset_class=/data_type=/SYMBOL.parquet`
//! objects from a local directory tree and decodes them into normalized market
//! events. This is the storage-side counterpart to [`crate::lake`]: the lake
//! adapter talks to a lake API and hands file *URLs* to an injected reader,
//! while this reader walks a materialized local directory directly, which is
//! what `backtest-job --data-dir <path>` consumes.
//!
//! The reader is synchronous and filesystem-only: it opens no sockets, reads no
//! wall clock, and produces events in the deterministic `(timestamp, sequence,
//! class priority)` order the replay bus requires. The companion
//! [`write_lake_partition`] encoder materializes the same layout for tests and
//! local fixtures.

use std::path::{Path, PathBuf};
use std::sync::Arc;

use arrow_array::{
    ArrayRef, Decimal128Array, RecordBatch, StringArray, TimestampNanosecondArray, UInt64Array,
};
use arrow_schema::{DataType as ArrowDataType, Field, Schema, TimeUnit};
use parquet::arrow::ArrowWriter;
use parquet::basic::Compression;
use parquet::file::properties::{WriterProperties, WriterVersion};

use crate::error::DataError;
use crate::events::{MarketEvent, QuoteEvent, TradeEvent};
use crate::lake::{partition_path, AssetClass, DataType, FileQueryFile};
use crate::replay::events_from_parquet;
use crate::symbol::canonical_symbol;
use crate::time::Date;

/// A synchronous reader over a materialized local market-lake directory.
///
/// The reader resolves the canonical Hive path for each `(date, asset class,
/// data type, symbol)` tuple and skips absent files, so a partial lake (only
/// quotes, or only some sessions) reads cleanly.
#[derive(Clone, Debug)]
pub struct LocalLakeReader {
    root: PathBuf,
}

impl LocalLakeReader {
    /// Build a reader rooted at one local lake directory.
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self { root: root.into() }
    }

    /// The reader's root directory.
    #[must_use]
    pub fn root(&self) -> &Path {
        &self.root
    }

    /// Read every quote and trade event for the given symbols and date range.
    ///
    /// # Errors
    ///
    /// Returns [`DataError`] when the date range is inverted, a symbol is not
    /// canonical, or a discovered Parquet object cannot be opened.
    pub fn read(
        &self,
        asset_class: AssetClass,
        start_date: Date,
        end_date: Date,
        symbols: &[String],
    ) -> Result<Vec<MarketEvent>, DataError> {
        if end_date < start_date {
            return Err(DataError::value("start_date must be on or before end_date"));
        }
        let mut events = Vec::new();
        let canonical: Vec<String> = symbols
            .iter()
            .map(|symbol| canonical_symbol(symbol))
            .collect::<Result<_, _>>()?;
        let mut day = start_date;
        loop {
            for symbol in &canonical {
                for data_type in [DataType::Quote, DataType::Trade] {
                    if let Some(mut file_events) =
                        self.read_partition(day, asset_class, data_type, symbol)?
                    {
                        events.append(&mut file_events);
                    }
                }
            }
            if day == end_date {
                break;
            }
            day = day.add_days(1);
        }
        events.sort_by_key(|event| event.ordering_key());
        Ok(events)
    }

    fn read_partition(
        &self,
        session_date: Date,
        asset_class: AssetClass,
        data_type: DataType,
        symbol: &str,
    ) -> Result<Option<Vec<MarketEvent>>, DataError> {
        let object_path = partition_path(session_date, asset_class, data_type, symbol)?;
        let path = self.root.join(&object_path);
        if !path.is_file() {
            return Ok(None);
        }
        let bytes = std::fs::read(&path).map_err(|error| {
            DataError::value(format!("read lake object {object_path}: {error}"))
        })?;
        let file = FileQueryFile {
            asset_class,
            data_type,
            symbol: symbol.to_string(),
            session_date,
            object_path,
            read_url: String::new(),
            byte_size: bytes.len() as u64,
            row_count: 0,
        };
        Ok(Some(events_from_parquet(&file, &bytes)?))
    }
}

/// Materialize one `date=/asset_class=/data_type=/SYMBOL.parquet` object.
///
/// The events whose variant matches `data_type` are encoded with the lake Arrow
/// schema and written under `root`. This is the deterministic fixture encoder
/// used by tests and local tooling.
///
/// # Errors
///
/// Returns [`DataError`] when the symbol is not canonical or the object cannot
/// be encoded or written.
pub fn write_lake_partition(
    root: &Path,
    session_date: Date,
    asset_class: AssetClass,
    data_type: DataType,
    symbol: &str,
    events: &[MarketEvent],
) -> Result<PathBuf, DataError> {
    let symbol = canonical_symbol(symbol)?;
    let object_path = partition_path(session_date, asset_class, data_type, &symbol)?;
    let path = root.join(&object_path);
    let payload = encode_events(data_type, events)?;
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|error| DataError::value(format!("create lake directory: {error}")))?;
    }
    std::fs::write(&path, payload)
        .map_err(|error| DataError::value(format!("write lake object {object_path}: {error}")))?;
    Ok(path)
}

fn encode_events(data_type: DataType, events: &[MarketEvent]) -> Result<Vec<u8>, DataError> {
    let schema = Arc::new(writer_schema(data_type)?);
    let selected: Vec<&MarketEvent> = events
        .iter()
        .filter(|event| {
            matches!(
                (data_type, event),
                (DataType::Quote, MarketEvent::Quote(_)) | (DataType::Trade, MarketEvent::Trade(_))
            )
        })
        .collect();
    if selected.is_empty() {
        return Err(DataError::value(
            "write_lake_partition requires at least one matching event",
        ));
    }
    let arrays = build_arrays(data_type, &selected)?;
    let batch = RecordBatch::try_new(Arc::clone(&schema), arrays)
        .map_err(|error| DataError::value(format!("record batch: {error}")))?;
    let properties = WriterProperties::builder()
        .set_compression(Compression::ZSTD(Default::default()))
        .set_writer_version(WriterVersion::PARQUET_2_0)
        .build();
    let mut buffer = Vec::new();
    {
        let mut writer = ArrowWriter::try_new(&mut buffer, Arc::clone(&schema), Some(properties))
            .map_err(|error| DataError::value(format!("parquet writer: {error}")))?;
        writer
            .write(&batch)
            .map_err(|error| DataError::value(format!("parquet write: {error}")))?;
        writer
            .close()
            .map_err(|error| DataError::value(format!("parquet close: {error}")))?;
    }
    Ok(buffer)
}

fn writer_schema(data_type: DataType) -> Result<Schema, DataError> {
    let timestamp = || ArrowDataType::Timestamp(TimeUnit::Nanosecond, Some("UTC".into()));
    let price = || ArrowDataType::Decimal128(38, 18);
    let fields = match data_type {
        DataType::Quote => vec![
            Field::new("timestamp", timestamp(), false),
            Field::new("symbol", ArrowDataType::Utf8, false),
            Field::new("event_id", ArrowDataType::Utf8, false),
            Field::new("sequence", ArrowDataType::UInt64, false),
            Field::new("bid_price", price(), false),
            Field::new("ask_price", price(), false),
            Field::new("bid_quantity", price(), false),
            Field::new("ask_quantity", price(), false),
            Field::new("venue", ArrowDataType::Utf8, true),
        ],
        DataType::Trade => vec![
            Field::new("timestamp", timestamp(), false),
            Field::new("symbol", ArrowDataType::Utf8, false),
            Field::new("event_id", ArrowDataType::Utf8, false),
            Field::new("sequence", ArrowDataType::UInt64, false),
            Field::new("price", price(), false),
            Field::new("quantity", price(), false),
            Field::new("venue", ArrowDataType::Utf8, true),
            Field::new("trade_id", ArrowDataType::UInt64, true),
        ],
        other => {
            return Err(DataError::value(format!(
                "local lake reader supports quote and trade partitions, not {}",
                other.value()
            )))
        }
    };
    Ok(Schema::new(fields))
}

fn build_arrays(data_type: DataType, events: &[&MarketEvent]) -> Result<Vec<ArrayRef>, DataError> {
    let timestamps: Vec<Option<i64>> = events
        .iter()
        .map(|event| Some(event.timestamp().to_nanos()))
        .collect();
    let timestamp = TimestampNanosecondArray::from(timestamps).with_timezone("UTC".to_string());
    let symbols: Vec<Option<String>> = events
        .iter()
        .map(|event| Some(event.symbol().to_string()))
        .collect();
    let event_ids: Vec<Option<String>> = events
        .iter()
        .map(|event| Some(event.event_id().to_string()))
        .collect();
    let sequences: Vec<Option<u64>> = events.iter().map(|event| Some(event.sequence())).collect();
    let mut arrays: Vec<ArrayRef> = vec![
        Arc::new(timestamp),
        Arc::new(StringArray::from(symbols)),
        Arc::new(StringArray::from(event_ids)),
        Arc::new(UInt64Array::from(sequences)),
    ];
    match data_type {
        DataType::Quote => {
            let mut bid = Vec::new();
            let mut ask = Vec::new();
            let mut bid_size = Vec::new();
            let mut ask_size = Vec::new();
            let mut venues = Vec::new();
            for event in events {
                let MarketEvent::Quote(QuoteEvent {
                    bid_price,
                    ask_price,
                    bid_size: bid_quantity,
                    ask_size: ask_quantity,
                    venue,
                    ..
                }) = event
                else {
                    return Err(DataError::value(
                        "quote partition carries a non-quote event",
                    ));
                };
                bid.push(Some(bid_price.unscaled()));
                ask.push(Some(ask_price.unscaled()));
                bid_size.push(Some(bid_quantity.unscaled()));
                ask_size.push(Some(ask_quantity.unscaled()));
                venues.push(venue.clone());
            }
            arrays.push(decimal_array(bid, 38, 18)?);
            arrays.push(decimal_array(ask, 38, 18)?);
            arrays.push(decimal_array(bid_size, 38, 18)?);
            arrays.push(decimal_array(ask_size, 38, 18)?);
            arrays.push(Arc::new(StringArray::from(venues)));
        }
        DataType::Trade => {
            let mut prices = Vec::new();
            let mut quantities = Vec::new();
            let mut venues = Vec::new();
            let mut trade_ids = Vec::new();
            for event in events {
                let MarketEvent::Trade(TradeEvent {
                    price,
                    quantity,
                    venue,
                    trade_id,
                    ..
                }) = event
                else {
                    return Err(DataError::value(
                        "trade partition carries a non-trade event",
                    ));
                };
                prices.push(Some(price.unscaled()));
                quantities.push(Some(quantity.unscaled()));
                venues.push(venue.clone());
                trade_ids.push(trade_id.clone());
            }
            arrays.push(decimal_array(prices, 38, 18)?);
            arrays.push(decimal_array(quantities, 38, 18)?);
            arrays.push(Arc::new(StringArray::from(venues)));
            let numeric_trade_ids: Vec<Option<u64>> = trade_ids
                .iter()
                .map(|value| value.as_deref().and_then(|text| text.parse::<u64>().ok()))
                .collect();
            arrays.push(Arc::new(UInt64Array::from(numeric_trade_ids)));
        }
        other => {
            return Err(DataError::value(format!(
                "local lake reader supports quote and trade partitions, not {}",
                other.value()
            )))
        }
    }
    Ok(arrays)
}

fn decimal_array(
    values: Vec<Option<i128>>,
    precision: u8,
    scale: i8,
) -> Result<ArrayRef, DataError> {
    let array = Decimal128Array::from(values)
        .with_precision_and_scale(precision, scale)
        .map_err(|error| DataError::value(format!("decimal array: {error}")))?;
    Ok(Arc::new(array))
}

#[cfg(test)]
mod tests {
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
        assert!(
            quote_path.ends_with("date=2024-01-02/asset_class=equity/data_type=quote/SPY.parquet")
        );
        assert!(
            trade_path.ends_with("date=2024-01-02/asset_class=equity/data_type=trade/SPY.parquet")
        );

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
}
