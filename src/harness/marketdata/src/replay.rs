//! Deterministic, bounded replay of market-lake quote and trade events.
//!
//! Port of `harness.data.replay`. The feed is an adapter rather than a storage
//! client: callers provide already-normalized event streams decoded from the
//! market-lake read route's Arrow IPC batches. Replay owns at most one
//! lookahead event per active source and merges events by the deterministic
//! `(timestamp, sequence, event-class priority, source order)` key.
//!
//! Fingerprints reproduce the Python reference exactly: a framed SHA-256 over
//! the selected sources, the ordered emitted rows, and the complete data
//! run. Frames are `u64` big-endian length prefixes of UTF-8 field text, exactly
//! matching `harness.data.replay._canonical_fields` and `_update_frame`.

use arrow_array::{
    Array, Decimal128Array, Int64Array, RecordBatch, StringArray, TimestampMicrosecondArray,
    TimestampNanosecondArray, UInt64Array,
};
use num::Decimal18;
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use sha2::{Digest, Sha256};

use crate::error::DataError;
use crate::events::{BarEvent, EventType, MarketEvent, QuoteEvent, TradeEvent};
use crate::lake::{DataType, ExchangeSession};
use crate::symbol::canonical_symbol;
use crate::time::DateTime;

/// Decode one Arrow IPC batch stream into normalized market events.
///
/// Row semantics mirror `harness.data.replay._row_event`: missing `event_id`
/// values fall back to `{symbol}#{row}`, `trade_id`/row-number fall back for
/// `sequence`, and the real lake schemas' provider column names (`bid_size`,
/// `size`, `exchange`, `bid_exchange`) are honored, so a route read decodes
/// exactly like the Python reference.
///
/// # Errors
///
/// Returns [`DataError`] when a batch is missing a required column, carries a
/// value of the wrong Arrow type, or the data type is not quote, trade, or bars.
pub fn events_from_arrow(
    data_type: DataType,
    symbol: &str,
    batches: &[RecordBatch],
) -> Result<Vec<MarketEvent>, DataError> {
    let mut events = Vec::new();
    let mut row_number = 0usize;
    for batch in batches {
        for row in 0..batch.num_rows() {
            events.push(row_event(data_type, symbol, batch, row, row_number)?);
            row_number += 1;
        }
    }
    Ok(events)
}

/// Decode one Parquet object into normalized market events.
///
/// This decodes the live recorder's own part format; market-lake reads go
/// through the Arrow IPC read route and [`events_from_arrow`] instead.
///
/// # Errors
///
/// Returns [`DataError`] under the same conditions as [`events_from_arrow`].
pub fn events_from_parquet(
    data_type: DataType,
    symbol: &str,
    bytes: &[u8],
) -> Result<Vec<MarketEvent>, DataError> {
    let builder = ParquetRecordBatchReaderBuilder::try_new(bytes::Bytes::copy_from_slice(bytes))
        .map_err(|error| DataError::value(format!("open parquet: {error}")))?;
    let reader = builder
        .build()
        .map_err(|error| DataError::value(format!("read parquet: {error}")))?;
    let mut events = Vec::new();
    let mut row_number = 0usize;
    for batch in reader {
        let batch = batch.map_err(|error| DataError::value(format!("parquet batch: {error}")))?;
        for row in 0..batch.num_rows() {
            events.push(row_event(data_type, symbol, &batch, row, row_number)?);
            row_number += 1;
        }
    }
    Ok(events)
}

/// Normalize one source row into the event model.
fn row_event(
    data_type: DataType,
    source_symbol: &str,
    batch: &RecordBatch,
    row: usize,
    row_number: usize,
) -> Result<MarketEvent, DataError> {
    let timestamp = read_timestamp(batch, "timestamp", row)?
        .ok_or_else(|| DataError::value("lake replay rows must contain datetime timestamps"))?;
    let raw_symbol = string_value(batch, "symbol", row)?
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| source_symbol.to_string());
    let symbol = canonical_symbol(&raw_symbol)?;
    let event_id = string_value(batch, "event_id", row)?
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| format!("{source_symbol}#{row_number}"));
    let sequence = integer_value(batch, "sequence", row)?
        .or(integer_value(batch, "trade_id", row)?)
        .unwrap_or(row_number as i64);
    let sequence = u64::try_from(sequence).unwrap_or(row_number as u64);
    match data_type {
        DataType::Quote => {
            let bid_price = decimal_value(batch, "bid_price", row)?
                .ok_or_else(|| DataError::value("quote row is missing bid_price"))?;
            let ask_price = decimal_value(batch, "ask_price", row)?
                .ok_or_else(|| DataError::value("quote row is missing ask_price"))?;
            let bid_size = decimal_value(batch, "bid_quantity", row)?
                .or(decimal_value(batch, "bid_size", row)?)
                .ok_or_else(|| DataError::value("quote row is missing bid size"))?;
            let ask_size = decimal_value(batch, "ask_quantity", row)?
                .or(decimal_value(batch, "ask_size", row)?)
                .ok_or_else(|| DataError::value("quote row is missing ask size"))?;
            let venue = string_value(batch, "venue", row)?
                .or(string_value(batch, "bid_exchange", row)?)
                .or(string_value(batch, "ask_exchange", row)?)
                .filter(|value| !value.is_empty());
            Ok(MarketEvent::Quote(QuoteEvent {
                event_id,
                timestamp,
                sequence,
                symbol,
                bid_price,
                bid_size,
                ask_price,
                ask_size,
                venue,
            }))
        }
        DataType::Trade => {
            let price = decimal_value(batch, "price", row)?
                .ok_or_else(|| DataError::value("trade row is missing price"))?;
            let quantity = decimal_value(batch, "quantity", row)?
                .or(decimal_value(batch, "size", row)?)
                .ok_or_else(|| DataError::value("trade row is missing quantity"))?;
            let venue = string_value(batch, "venue", row)?
                .or(string_value(batch, "exchange", row)?)
                .filter(|value| !value.is_empty());
            let trade_id = string_value(batch, "trade_id", row)?.filter(|value| !value.is_empty());
            Ok(MarketEvent::Trade(TradeEvent {
                event_id,
                timestamp,
                sequence,
                symbol,
                price,
                quantity,
                venue,
                trade_id,
            }))
        }
        DataType::Bars => {
            let open = decimal_value(batch, "open", row)?
                .ok_or_else(|| DataError::value("bar row is missing open"))?;
            let high = decimal_value(batch, "high", row)?
                .ok_or_else(|| DataError::value("bar row is missing high"))?;
            let low = decimal_value(batch, "low", row)?
                .ok_or_else(|| DataError::value("bar row is missing low"))?;
            let close = decimal_value(batch, "close", row)?
                .ok_or_else(|| DataError::value("bar row is missing close"))?;
            let volume = decimal_value(batch, "volume", row)?
                .ok_or_else(|| DataError::value("bar row is missing volume"))?;
            Ok(MarketEvent::Bar(BarEvent {
                event_id,
                timestamp,
                sequence,
                symbol,
                open,
                high,
                low,
                close,
                volume,
            }))
        }
        _ => Err(DataError::value(
            "replay files must be quote, trade, or bars partitions",
        )),
    }
}

fn column_index(batch: &arrow_array::RecordBatch, name: &str) -> Option<usize> {
    batch
        .schema()
        .fields()
        .iter()
        .position(|field| field.name() == name)
}

fn string_value(
    batch: &arrow_array::RecordBatch,
    name: &str,
    row: usize,
) -> Result<Option<String>, DataError> {
    let Some(index) = column_index(batch, name) else {
        return Ok(None);
    };
    let column = batch.column(index);
    if column.is_null(row) {
        return Ok(None);
    }
    if let Some(array) = column.as_any().downcast_ref::<StringArray>() {
        return Ok(Some(array.value(row).to_string()));
    }
    if let Some(array) = column.as_any().downcast_ref::<UInt64Array>() {
        return Ok(Some(array.value(row).to_string()));
    }
    if let Some(array) = column.as_any().downcast_ref::<Int64Array>() {
        return Ok(Some(array.value(row).to_string()));
    }
    if let Some(array) = column.as_any().downcast_ref::<arrow_array::UInt32Array>() {
        return Ok(Some(array.value(row).to_string()));
    }
    Err(DataError::value(format!(
        "Parquet column {name} must be a UTF-8 string"
    )))
}

fn integer_value(
    batch: &arrow_array::RecordBatch,
    name: &str,
    row: usize,
) -> Result<Option<i64>, DataError> {
    let Some(index) = column_index(batch, name) else {
        return Ok(None);
    };
    let column = batch.column(index);
    if column.is_null(row) {
        return Ok(None);
    }
    if let Some(array) = column.as_any().downcast_ref::<UInt64Array>() {
        return Ok(Some(array.value(row) as i64));
    }
    if let Some(array) = column.as_any().downcast_ref::<Int64Array>() {
        return Ok(Some(array.value(row)));
    }
    if let Some(array) = column.as_any().downcast_ref::<arrow_array::UInt32Array>() {
        return Ok(Some(i64::from(array.value(row))));
    }
    Err(DataError::value(format!(
        "Parquet column {name} must be an integer"
    )))
}

fn decimal_value(
    batch: &arrow_array::RecordBatch,
    name: &str,
    row: usize,
) -> Result<Option<Decimal18>, DataError> {
    let Some(index) = column_index(batch, name) else {
        return Ok(None);
    };
    let column = batch.column(index);
    if column.is_null(row) {
        return Ok(None);
    }
    if let Some(array) = column.as_any().downcast_ref::<Decimal128Array>() {
        let unscaled = array.value(row);
        let adjusted = rescale(unscaled, array.scale())?;
        return Ok(Some(Decimal18::from_unscaled(adjusted)?));
    }
    if let Some(array) = column.as_any().downcast_ref::<arrow_array::UInt32Array>() {
        return Ok(Some(Decimal18::from_i64(i64::from(array.value(row)))));
    }
    if let Some(array) = column.as_any().downcast_ref::<UInt64Array>() {
        return Ok(Some(Decimal18::from_i64(array.value(row) as i64)));
    }
    if let Some(array) = column.as_any().downcast_ref::<Int64Array>() {
        return Ok(Some(Decimal18::from_i64(array.value(row))));
    }
    Err(DataError::value(format!(
        "Parquet column {name} must be a decimal128 or integer"
    )))
}

/// Rescale a decimal128 unscaled value to the crate's fixed scale 18.
fn rescale(unscaled: i128, scale: i8) -> Result<i128, DataError> {
    let delta = 18i32 - i32::from(scale);
    if delta == 0 {
        return Ok(unscaled);
    }
    if delta > 0 {
        let factor = 10i128
            .checked_pow(delta as u32)
            .ok_or_else(|| DataError::value("decimal rescale overflow"))?;
        unscaled
            .checked_mul(factor)
            .ok_or_else(|| DataError::value("decimal rescale overflow"))
    } else {
        let factor = 10i128
            .checked_pow((-delta) as u32)
            .ok_or_else(|| DataError::value("decimal rescale overflow"))?;
        Ok(unscaled / factor)
    }
}

fn read_timestamp(
    batch: &arrow_array::RecordBatch,
    name: &str,
    row: usize,
) -> Result<Option<DateTime>, DataError> {
    let Some(index) = column_index(batch, name) else {
        return Ok(None);
    };
    let column = batch.column(index);
    if column.is_null(row) {
        return Ok(None);
    }
    if let Some(array) = column.as_any().downcast_ref::<TimestampNanosecondArray>() {
        return Ok(Some(DateTime::from_nanos(array.value(row))));
    }
    if let Some(array) = column.as_any().downcast_ref::<TimestampMicrosecondArray>() {
        return Ok(Some(DateTime::from_micros(array.value(row))));
    }
    Err(DataError::value(format!(
        "Parquet column {name} must be a timestamp"
    )))
}

/// One explicitly ordered, per-symbol event stream.
#[derive(Clone)]
pub struct ReplayStream {
    /// Canonical symbol.
    pub symbol: String,
    /// Optional data-type constraint.
    pub data_type: Option<DataType>,
    /// The ordered events.
    pub events: Vec<MarketEvent>,
}

impl ReplayStream {
    /// Build a validated stream.
    pub fn new(
        symbol: &str,
        data_type: Option<DataType>,
        events: Vec<MarketEvent>,
    ) -> Result<Self, DataError> {
        Ok(Self {
            symbol: canonical_symbol(symbol)?,
            data_type,
            events,
        })
    }
}

/// Stable hashes for the selected files, emitted rows, and complete data.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ReplayFingerprints {
    /// Selected-file/source fingerprint.
    pub file: String,
    /// Ordered emitted-row fingerprint.
    pub row: String,
    /// Complete replay fingerprint.
    pub data: String,
}

impl ReplayFingerprints {
    /// The selected-file fingerprint.
    pub fn file_fingerprint(&self) -> &str {
        &self.file
    }

    /// The ordered emitted-row fingerprint.
    pub fn row_fingerprint(&self) -> &str {
        &self.row
    }

    /// The complete replay fingerprint.
    pub fn data_fingerprint(&self) -> &str {
        &self.data
    }
}

/// One explicit stream source used by the selected-source fingerprint.
struct StreamEntry {
    order: usize,
    symbol: String,
    data_type: Option<DataType>,
}

struct Source {
    symbol: String,
    data_type: Option<DataType>,
    session_date: Option<crate::time::Date>,
    order: usize,
    events: Vec<MarketEvent>,
    cursor: usize,
}

impl Source {
    fn peek(&self) -> Option<&MarketEvent> {
        self.events.get(self.cursor)
    }

    fn pop(&mut self) -> Option<MarketEvent> {
        let event = self.events.get(self.cursor).cloned();
        if event.is_some() {
            self.cursor += 1;
        }
        event
    }
}

/// Stream deterministic market events one explicit session at a time.
pub struct LakeReplayFeed {
    file_fingerprint: String,
    last_fingerprints: ReplayFingerprints,
    sessions: Vec<ExchangeSession>,
    sources: Vec<Source>,
}

impl LakeReplayFeed {
    /// Build a feed over explicit sessions and decoded event streams.
    ///
    /// Route reads are decoded with [`events_from_arrow`] and passed as
    /// [`ReplayStream`]s; the feed owns no file or path surface.
    pub fn new(
        sessions: Vec<ExchangeSession>,
        streams: Vec<ReplayStream>,
    ) -> Result<Self, DataError> {
        let sessions = normalize_sessions(sessions)?;
        let mut stream_entries: Vec<StreamEntry> = Vec::new();
        let mut sources: Vec<Source> = Vec::new();
        for (order, stream) in streams.into_iter().enumerate() {
            validate_stream(&stream)?;
            stream_entries.push(StreamEntry {
                order,
                symbol: stream.symbol.clone(),
                data_type: stream.data_type,
            });
            sources.push(Source {
                symbol: stream.symbol,
                data_type: stream.data_type,
                session_date: None,
                order,
                events: stream.events,
                cursor: 0,
            });
        }
        let file_fingerprint = file_fingerprint(&stream_entries);
        let last_fingerprints = ReplayFingerprints {
            file: file_fingerprint.clone(),
            row: hex_string(&Sha256::new().finalize()),
            data: empty_data_fingerprint(&file_fingerprint, &sessions),
        };
        Ok(Self {
            file_fingerprint,
            last_fingerprints,
            sessions,
            sources,
        })
    }

    /// The explicit replay sessions in chronological order.
    pub fn sessions(&self) -> &[ExchangeSession] {
        &self.sessions
    }

    /// The stable selected-file/source fingerprint.
    pub fn file_fingerprint(&self) -> &str {
        &self.file_fingerprint
    }

    /// All latest replay fingerprints.
    pub fn fingerprints(&self) -> &ReplayFingerprints {
        &self.last_fingerprints
    }

    /// Yield merged events without collecting a full run.
    pub fn events(&mut self) -> Result<Vec<MarketEvent>, DataError> {
        let mut state = FingerprintState::new(&self.file_fingerprint, &self.sessions);
        let mut emitted = Vec::new();
        for index in 0..self.sessions.len() {
            let session = self.sessions[index];
            let mut merged = self.merge_session(&session)?;
            for event in merged.drain(..) {
                state.add(&event);
                emitted.push(event);
            }
        }
        self.last_fingerprints = state.fingerprints(&self.file_fingerprint);
        Ok(emitted)
    }

    fn merge_session(&mut self, session: &ExchangeSession) -> Result<Vec<MarketEvent>, DataError> {
        let mut collected: Vec<(MarketEvent, usize)> = Vec::new();
        for source in &mut self.sources {
            if let Some(date) = source.session_date {
                if date != session.session_date {
                    continue;
                }
            }
            while let Some(event) = source.peek() {
                if event.timestamp() < session.open_time {
                    source.pop();
                } else {
                    break;
                }
            }
            while let Some(event) = source.peek() {
                if event.timestamp() >= session.close_time {
                    break;
                }
                validate_source_event(event, source)?;
                let order = source.order;
                if let Some(event) = source.pop() {
                    collected.push((event, order));
                }
            }
        }
        collected.sort_by(|a, b| {
            a.0.ordering_key()
                .cmp(&b.0.ordering_key())
                .then(a.1.cmp(&b.1))
        });
        Ok(collected.into_iter().map(|(event, _)| event).collect())
    }
}

fn normalize_sessions(sessions: Vec<ExchangeSession>) -> Result<Vec<ExchangeSession>, DataError> {
    if sessions.is_empty() {
        return Err(DataError::value("at least one replay session is required"));
    }
    let mut ordered = sessions;
    ordered.sort_by_key(|session| session.session_date);
    for window in ordered.windows(2) {
        if window[0].session_date == window[1].session_date {
            return Err(DataError::value(
                "replay sessions must not contain duplicate dates",
            ));
        }
    }
    for session in &ordered {
        if session.close_time <= session.open_time {
            return Err(DataError::value(
                "session close_time must be after open_time",
            ));
        }
    }
    Ok(ordered)
}

fn validate_stream(stream: &ReplayStream) -> Result<(), DataError> {
    let expected = match stream.data_type {
        Some(DataType::Quote) => Some(EventType::Quote),
        Some(DataType::Trade) => Some(EventType::Trade),
        Some(DataType::Bars) => Some(EventType::Bar),
        _ => None,
    };
    let mut last = None;
    for event in &stream.events {
        if event.symbol() != stream.symbol {
            return Err(DataError::value(format!(
                "replay event symbol {} does not match source {:?}",
                event.symbol(),
                stream.symbol
            )));
        }
        if let Some(expected) = expected {
            if event.event_type() != expected {
                return Err(DataError::value(
                    "replay event type does not match its source data type",
                ));
            }
        }
        let key = event.ordering_key();
        if let Some(previous) = last {
            if key < previous {
                return Err(DataError::value(
                    "each replay source must be ordered by timestamp and sequence",
                ));
            }
        }
        last = Some(key);
    }
    Ok(())
}

fn validate_source_event(event: &MarketEvent, source: &Source) -> Result<(), DataError> {
    if event.symbol() != source.symbol {
        return Err(DataError::value(format!(
            "replay event symbol {} does not match source {:?}",
            event.symbol(),
            source.symbol
        )));
    }
    let expected = match source.data_type {
        Some(DataType::Quote) => Some(EventType::Quote),
        Some(DataType::Trade) => Some(EventType::Trade),
        Some(DataType::Bars) => Some(EventType::Bar),
        _ => None,
    };
    if let Some(expected) = expected {
        if event.event_type() != expected {
            return Err(DataError::value(
                "replay event type does not match its source data type",
            ));
        }
    }
    Ok(())
}

/// A stable SHA-256 over framed UTF-8 fields, hex encoded.
pub(crate) fn canonical_fields(values: &[String]) -> Vec<u8> {
    let mut output = Vec::new();
    for value in values {
        let payload = value.as_bytes();
        output.extend_from_slice(&(payload.len() as u64).to_be_bytes());
        output.extend_from_slice(payload);
    }
    output
}

/// Push one Python `_update_frame`: a `u64` big-endian length then the payload.
pub(crate) fn update_frame(digest: &mut Sha256, payload: &[u8]) {
    digest.update((payload.len() as u64).to_be_bytes());
    digest.update(payload);
}

/// Lower-hex encode a digest.
pub(crate) fn hex_string(bytes: &[u8]) -> String {
    hex::encode(bytes)
}

/// The streaming fingerprint accumulator shared by replay and options replay.
struct FingerprintState {
    row: Sha256,
    data: Sha256,
}

impl FingerprintState {
    fn new(file_fingerprint: &str, sessions: &[ExchangeSession]) -> Self {
        let mut data = Sha256::new();
        data.update(b"fca-lake-replay-data-v1\0");
        update_frame(&mut data, file_fingerprint.as_bytes());
        for session in sessions {
            update_frame(
                &mut data,
                &canonical_fields(&[
                    session.session_date.isoformat(),
                    session.open_time.isoformat_micros_utc(),
                    session.close_time.isoformat_micros_utc(),
                ]),
            );
        }
        Self {
            row: Sha256::new(),
            data,
        }
    }

    fn add(&mut self, event: &MarketEvent) {
        let payload = event_fingerprint(event);
        update_frame(&mut self.row, &payload);
        update_frame(&mut self.data, &payload);
    }

    fn fingerprints(&self, file_fingerprint: &str) -> ReplayFingerprints {
        ReplayFingerprints {
            file: file_fingerprint.to_string(),
            row: hex_string(&self.row.clone().finalize()),
            data: hex_string(&self.data.clone().finalize()),
        }
    }
}

fn event_fingerprint(event: &MarketEvent) -> Vec<u8> {
    match event {
        MarketEvent::Quote(quote) => canonical_fields(&[
            "quote".to_string(),
            quote.event_id.clone(),
            quote.timestamp.isoformat_micros_utc(),
            quote.sequence.to_string(),
            quote.symbol.clone(),
            quote.bid_price.to_string(),
            quote.bid_size.to_string(),
            quote.ask_price.to_string(),
            quote.ask_size.to_string(),
            quote.venue.clone().unwrap_or_default(),
        ]),
        MarketEvent::Trade(trade) => canonical_fields(&[
            "trade".to_string(),
            trade.event_id.clone(),
            trade.timestamp.isoformat_micros_utc(),
            trade.sequence.to_string(),
            trade.symbol.clone(),
            trade.price.to_string(),
            trade.quantity.to_string(),
            trade.venue.clone().unwrap_or_default(),
            trade.trade_id.clone().unwrap_or_default(),
        ]),
        MarketEvent::Bar(bar) => canonical_fields(&[
            "bar".to_string(),
            bar.event_id.clone(),
            bar.timestamp.isoformat_micros_utc(),
            bar.sequence.to_string(),
            bar.symbol.clone(),
            bar.open.to_string(),
            bar.high.to_string(),
            bar.low.to_string(),
            bar.close.to_string(),
            bar.volume.to_string(),
        ]),
    }
}

fn file_fingerprint(streams: &[StreamEntry]) -> String {
    let mut digest = Sha256::new();
    digest.update(b"fca-lake-replay-files-v1\0");
    for stream in streams {
        update_frame(
            &mut digest,
            &canonical_fields(&[
                "stream".to_string(),
                stream.order.to_string(),
                stream.symbol.clone(),
                stream
                    .data_type
                    .map(|data_type| data_type.value().to_string())
                    .unwrap_or_else(|| "None".to_string()),
            ]),
        );
    }
    hex_string(&digest.finalize())
}

fn empty_data_fingerprint(file_fingerprint: &str, sessions: &[ExchangeSession]) -> String {
    let state = FingerprintState::new(file_fingerprint, sessions);
    hex_string(&state.data.clone().finalize())
}
