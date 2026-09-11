//! Deterministic, bounded replay of market-lake quote and trade partitions.
//!
//! Port of `harness.data.replay`. The feed is an adapter rather than a storage
//! client: callers provide already-normalized event streams or a reader for the
//! file objects returned by the market-lake contract. Replay owns at most one
//! lookahead event per active source and merges events by the deterministic
//! `(timestamp, sequence, event-class priority, source order)` key.
//!
//! Fingerprints reproduce the Python reference exactly: a framed SHA-256 over
//! the selected sources/files, the ordered emitted rows, and the complete data
//! run. Frames are `u64` big-endian length prefixes of UTF-8 field text, exactly
//! matching `harness.data.replay._canonical_fields` and `_update_frame`.

use std::collections::HashMap;
use std::path::PathBuf;
use std::rc::Rc;

use arrow_array::{
    Array, Decimal128Array, Int64Array, StringArray, TimestampMicrosecondArray,
    TimestampNanosecondArray, UInt64Array,
};
use bytes::Bytes;
use num::Decimal18;
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use sha2::{Digest, Sha256};

use crate::error::DataError;
use crate::events::{EventType, MarketEvent, QuoteEvent, TradeEvent};
use crate::lake::{DataType, ExchangeSession, FileQueryFile};
use crate::symbol::canonical_symbol;
use crate::time::DateTime;

/// Injected reader for one complete lake file.
pub trait ParquetReader {
    /// Return the events normalized from one lake file.
    fn read(&self, file: &FileQueryFile) -> Result<Vec<MarketEvent>, DataError>;
}

/// A simple reader that returns a fixed event vector for every file.
#[derive(Clone, Debug, Default)]
pub struct StaticReader {
    /// Events returned for each file.
    pub events: Vec<MarketEvent>,
}

impl ParquetReader for StaticReader {
    fn read(&self, _file: &FileQueryFile) -> Result<Vec<MarketEvent>, DataError> {
        Ok(self.events.clone())
    }
}

/// Byte-level read boundary for one lake object.
pub trait LakeObjectSource {
    /// Return the complete bytes of one lake object.
    fn read(&self, file: &FileQueryFile) -> Result<Vec<u8>, DataError>;
}

/// An in-memory object source keyed by canonical object path.
#[derive(Clone, Debug, Default)]
pub struct MemoryObjectSource {
    /// Object bytes keyed by `FileQueryFile::object_path`.
    pub objects: HashMap<String, Vec<u8>>,
}

impl LakeObjectSource for MemoryObjectSource {
    fn read(&self, file: &FileQueryFile) -> Result<Vec<u8>, DataError> {
        self.objects.get(&file.object_path).cloned().ok_or_else(|| {
            DataError::value(format!("no object registered for {}", file.object_path))
        })
    }
}

/// A filesystem object source rooted at one directory.
#[derive(Clone, Debug)]
pub struct LocalObjectSource {
    root: PathBuf,
}

impl LocalObjectSource {
    /// Build a source rooted at `root`.
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self { root: root.into() }
    }
}

impl LakeObjectSource for LocalObjectSource {
    fn read(&self, file: &FileQueryFile) -> Result<Vec<u8>, DataError> {
        let target = self.root.join(&file.object_path);
        std::fs::read(&target)
            .map_err(|error| DataError::value(format!("read object {}: {error}", file.object_path)))
    }
}

/// A real Parquet reader that decodes the lake Arrow schemas into market events.
pub struct ParquetFileReader {
    /// Object byte source.
    pub source: Rc<dyn LakeObjectSource>,
}

impl ParquetFileReader {
    /// Build a reader over `source`.
    pub fn new(source: Rc<dyn LakeObjectSource>) -> Self {
        Self { source }
    }
}

impl ParquetReader for ParquetFileReader {
    fn read(&self, file: &FileQueryFile) -> Result<Vec<MarketEvent>, DataError> {
        let bytes = self.source.read(file)?;
        events_from_parquet(file, &bytes)
    }
}

/// Decode one Parquet object into normalized market events.
///
/// Row semantics mirror `harness.data.replay._row_event`, including the
/// generated `{object_path}#{row}` event id and `trade_id`/row sequence
/// fallbacks, so fingerprints computed over file-backed replay match Python.
pub fn events_from_parquet(
    file: &FileQueryFile,
    bytes: &[u8],
) -> Result<Vec<MarketEvent>, DataError> {
    let builder = ParquetRecordBatchReaderBuilder::try_new(Bytes::copy_from_slice(bytes))
        .map_err(|error| DataError::value(format!("open parquet: {error}")))?;
    let reader = builder
        .build()
        .map_err(|error| DataError::value(format!("read parquet: {error}")))?;
    let mut events = Vec::new();
    let mut row_number = 0usize;
    for batch in reader {
        let batch = batch.map_err(|error| DataError::value(format!("parquet batch: {error}")))?;
        for row in 0..batch.num_rows() {
            events.push(parquet_row_event(file, &batch, row, row_number)?);
            row_number += 1;
        }
    }
    Ok(events)
}

fn parquet_row_event(
    file: &FileQueryFile,
    batch: &arrow_array::RecordBatch,
    row: usize,
    row_number: usize,
) -> Result<MarketEvent, DataError> {
    let timestamp = read_timestamp(batch, "timestamp", row)?
        .ok_or_else(|| DataError::value("Parquet replay rows must contain datetime timestamps"))?;
    let raw_symbol = string_value(batch, "symbol", row)?
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| file.symbol.clone());
    let symbol = canonical_symbol(&raw_symbol)?;
    let event_id = string_value(batch, "event_id", row)?
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| format!("{}#{}", file.object_path, row_number));
    let sequence = integer_value(batch, "sequence", row)?
        .or(integer_value(batch, "trade_id", row)?)
        .unwrap_or(row_number as i64);
    let sequence = u64::try_from(sequence).unwrap_or(row_number as u64);
    match file.data_type {
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
        _ => Err(DataError::value(
            "replay files must be quote or trade partitions",
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
    /// Build a feed over explicit sessions, optional streams, and files.
    pub fn new(
        sessions: Vec<ExchangeSession>,
        streams: Vec<ReplayStream>,
        files: Vec<FileQueryFile>,
        reader: Option<Rc<dyn ParquetReader>>,
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
        let mut sorted_files = files;
        for file in &sorted_files {
            validate_file(file)?;
        }
        sorted_files.sort_by(|a, b| {
            (
                a.session_date,
                &a.symbol,
                a.data_type.value(),
                &a.object_path,
            )
                .cmp(&(
                    b.session_date,
                    &b.symbol,
                    b.data_type.value(),
                    &b.object_path,
                ))
        });
        validate_file_sessions(&sorted_files, &sessions)?;
        if !sorted_files.is_empty() && reader.is_none() {
            return Err(DataError::type_error(
                "reader is required when files are provided",
            ));
        }
        let file_fingerprint = file_fingerprint(&stream_entries, &sorted_files)?;
        let last_fingerprints = ReplayFingerprints {
            file: file_fingerprint.clone(),
            row: hex_string(&Sha256::new().finalize()),
            data: empty_data_fingerprint(&file_fingerprint, &sessions),
        };
        if let Some(reader) = reader {
            for (index, file) in sorted_files.iter().enumerate() {
                sources.push(Source {
                    symbol: canonical_symbol(&file.symbol)?,
                    data_type: Some(file.data_type),
                    session_date: Some(file.session_date),
                    order: stream_entries.len() + index,
                    events: reader.read(file)?,
                    cursor: 0,
                });
            }
        }
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

fn validate_file(file: &FileQueryFile) -> Result<(), DataError> {
    if !matches!(file.data_type, DataType::Quote | DataType::Trade) {
        return Err(DataError::value(
            "replay files must be quote or trade partitions",
        ));
    }
    let expected = crate::lake::partition_path(
        file.session_date,
        file.asset_class,
        file.data_type,
        &file.symbol,
    )?;
    if file.object_path != expected {
        return Err(DataError::value(
            "replay file has a non-canonical lake partition path",
        ));
    }
    Ok(())
}

fn validate_file_sessions(
    files: &[FileQueryFile],
    sessions: &[ExchangeSession],
) -> Result<(), DataError> {
    let expected: Vec<crate::time::Date> = sessions
        .iter()
        .map(|session| session.session_date)
        .collect();
    let mut seen: Vec<(crate::time::Date, String, DataType)> = Vec::new();
    for file in files {
        if !expected.contains(&file.session_date) {
            return Err(DataError::value(
                "replay file session is not part of the replay",
            ));
        }
        let symbol = canonical_symbol(&file.symbol)?;
        let key = (file.session_date, symbol, file.data_type);
        if seen.contains(&key) {
            return Err(DataError::value(
                "replay files must not duplicate a symbol/data-type session",
            ));
        }
        seen.push(key);
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
    }
}

fn file_fingerprint(streams: &[StreamEntry], files: &[FileQueryFile]) -> Result<String, DataError> {
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
    for file in files {
        update_frame(
            &mut digest,
            &canonical_fields(&[
                "file".to_string(),
                file.session_date.isoformat(),
                canonical_symbol(&file.symbol)?,
                file.data_type.value().to_string(),
                file.object_path.clone(),
                file.row_count.to_string(),
                file.byte_size.to_string(),
                // The Rust lake model does not carry the optional checksum the
                // Python FileQueryFile exposes; `checksum or ""` is the empty
                // string whenever the provider did not issue one.
                String::new(),
            ]),
        );
    }
    Ok(hex_string(&digest.finalize()))
}

fn empty_data_fingerprint(file_fingerprint: &str, sessions: &[ExchangeSession]) -> String {
    let state = FingerprintState::new(file_fingerprint, sessions);
    hex_string(&state.data.clone().finalize())
}

#[cfg(test)]
mod tests {
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
        let mut feed =
            LakeReplayFeed::new(vec![session(0)], vec![stream], Vec::new(), None).unwrap();
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
        let mut feed = LakeReplayFeed::new(vec![session], vec![stream], Vec::new(), None).unwrap();
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
}
