//! Deterministic, bounded replay of market-lake quote and trade partitions.
//!
//! Port of `harness.data.replay`. The feed is an adapter rather than a storage
//! client: callers provide already-normalized event streams or a reader for the
//! file objects returned by the market-lake contract. Replay owns at most one
//! lookahead event per active source and merges events by the deterministic
//! `(timestamp, sequence, event-class priority, source order)` key.

use std::rc::Rc;

use crate::error::DataError;
use crate::events::{EventType, MarketEvent};
use crate::lake::{DataType, ExchangeSession, FileQueryFile};
use crate::symbol::canonical_symbol;

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

struct Source {
    symbol: String,
    data_type: Option<DataType>,
    session_date: Option<crate::time::Date>,
    order: usize,
    name: String,
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
        let mut sources: Vec<Source> = Vec::new();
        for (order, stream) in streams.into_iter().enumerate() {
            let data_type = match (&stream.data_type, &stream.events) {
                (Some(DataType::Quote), _) => Some(DataType::Quote),
                (Some(DataType::Trade), _) => Some(DataType::Trade),
                _ => None,
            };
            validate_stream(&stream)?;
            sources.push(Source {
                symbol: stream.symbol.clone(),
                data_type,
                session_date: None,
                order,
                name: format!("stream:{order}:{}", stream.symbol),
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
        if !sorted_files.is_empty() && reader.is_none() {
            return Err(DataError::type_error(
                "reader is required when files are provided",
            ));
        }
        if let Some(reader) = reader {
            for (index, file) in sorted_files.iter().enumerate() {
                sources.push(Source {
                    symbol: canonical_symbol(&file.symbol)?,
                    data_type: Some(file.data_type),
                    session_date: Some(file.session_date),
                    order: sources.len() + index,
                    name: file.object_path.clone(),
                    events: reader.read(file)?,
                    cursor: 0,
                });
            }
        }
        let file_fingerprint = file_fingerprint(&sources);
        let sessions_for_empty: Vec<_> = sessions.clone();
        let last_fingerprints = ReplayFingerprints {
            file: file_fingerprint.clone(),
            row: fingerprint_hex(&[]),
            data: empty_data_fingerprint(&file_fingerprint, &sessions_for_empty),
        };
        // Keep the source `order` dense and session-scoped file sources distinct.
        for (index, source) in sources.iter_mut().enumerate() {
            source.order = index;
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
        let mut emitted = Vec::new();
        for index in 0..self.sessions.len() {
            let session = self.sessions[index];
            let mut merged = self.merge_session(&session)?;
            emitted.append(&mut merged);
        }
        let row = fingerprint_events(&emitted);
        self.last_fingerprints = ReplayFingerprints {
            file: self.file_fingerprint.clone(),
            row: row.clone(),
            data: fingerprint_hex(&[canonical_fields(&[self.file_fingerprint.clone(), row])]),
        };
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

/// A stable 64-bit FNV-1a fingerprint, hex encoded.
pub(crate) fn fingerprint_hex(frames: &[Vec<u8>]) -> String {
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
    for frame in frames {
        for byte in (frame.len() as u64).to_be_bytes() {
            hash ^= u64::from(byte);
            hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
        }
        for byte in frame {
            hash ^= u64::from(*byte);
            hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
        }
    }
    format!("{hash:016x}")
}

pub(crate) fn canonical_fields(values: &[String]) -> Vec<u8> {
    let mut output = Vec::new();
    for value in values {
        let payload = value.as_bytes();
        output.extend_from_slice(&(payload.len() as u64).to_be_bytes());
        output.extend_from_slice(payload);
    }
    output
}

fn event_fingerprint(event: &MarketEvent) -> Vec<u8> {
    match event {
        MarketEvent::Quote(quote) => canonical_fields(&[
            "quote".to_string(),
            quote.event_id.clone(),
            quote.timestamp.to_string(),
            quote.sequence.to_string(),
            quote.symbol.clone(),
            quote.bid_price.to_string(),
            quote.ask_price.to_string(),
        ]),
        MarketEvent::Trade(trade) => canonical_fields(&[
            "trade".to_string(),
            trade.event_id.clone(),
            trade.timestamp.to_string(),
            trade.sequence.to_string(),
            trade.symbol.clone(),
            trade.price.to_string(),
            trade.quantity.to_string(),
        ]),
    }
}

fn fingerprint_events(events: &[MarketEvent]) -> String {
    let frames: Vec<Vec<u8>> = events.iter().map(event_fingerprint).collect();
    fingerprint_hex(&frames)
}

fn file_fingerprint(sources: &[Source]) -> String {
    let frames: Vec<Vec<u8>> = sources
        .iter()
        .map(|source| {
            canonical_fields(&[
                "source".to_string(),
                source.order.to_string(),
                source.name.clone(),
                source.symbol.clone(),
                source
                    .data_type
                    .map(|data_type| data_type.value().to_string())
                    .unwrap_or_default(),
            ])
        })
        .collect();
    fingerprint_hex(&frames)
}

fn empty_data_fingerprint(file_fingerprint: &str, sessions: &[ExchangeSession]) -> String {
    let mut frames: Vec<Vec<u8>> = vec![canonical_fields(&[file_fingerprint.to_string()])];
    for session in sessions {
        frames.push(canonical_fields(&[
            session.session_date.isoformat(),
            session.open_time.to_string(),
            session.close_time.to_string(),
        ]));
    }
    fingerprint_hex(&frames)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::events::TradeEvent;
    use crate::time::{Date, DateTime};
    use harness_num::Decimal18;
    use std::str::FromStr;

    fn instant(seconds: i64) -> DateTime {
        DateTime::from_micros(seconds * 1_000_000)
    }

    fn trade(symbol: &str, seconds: i64, sequence: u64) -> MarketEvent {
        MarketEvent::Trade(TradeEvent::new(
            symbol.to_string(),
            instant(seconds),
            sequence,
            Decimal18::from_str("10").unwrap(),
            Decimal18::from_str("1").unwrap(),
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
}
