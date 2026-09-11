//! Day-bounded, deterministic replay of historical option market data.
//!
//! Port of `harness.data.options_replay`. Options replay navigates the
//! market-lake `date/asset_class/data_type` partition layout for option quote
//! and trade files and streams each session through the bounded replay
//! contract. Contract selection is point in time through an injected
//! [`OptionUniverseView`].

use std::rc::Rc;

use crate::catalog::ContractRecord;
use crate::error::DataError;
use crate::events::MarketEvent;
use crate::lake::{AssetClass, DataType, ExchangeSession, FileQueryFile};
use crate::options_universe::OptionUniverseView;
use crate::replay::{
    canonical_fields, hex_string, update_frame, LakeReplayFeed, ParquetReader, ReplayFingerprints,
    ReplayStream,
};
use crate::symbol::canonical_symbol;
use crate::time::Date;
use sha2::{Digest, Sha256};

/// Read boundary over the market-lake option partition layout.
pub trait OptionPartitionPort {
    /// Return the option partition for one contract session, if present.
    fn file(
        &self,
        session_date: Date,
        data_type: DataType,
        symbol: &str,
    ) -> Result<Option<FileQueryFile>, DataError>;
}

/// A trivial in-memory partition port for tests and downstream fixtures.
#[derive(Clone, Debug, Default)]
pub struct MemoryOptionPartitionPort {
    /// Available files.
    pub files: Vec<FileQueryFile>,
}

impl OptionPartitionPort for MemoryOptionPartitionPort {
    fn file(
        &self,
        session_date: Date,
        data_type: DataType,
        symbol: &str,
    ) -> Result<Option<FileQueryFile>, DataError> {
        let target = canonical_symbol(symbol)?;
        Ok(self
            .files
            .iter()
            .find(|file| {
                file.session_date == session_date
                    && file.data_type == data_type
                    && file.symbol == target
            })
            .cloned())
    }
}

/// One resolved session: selected contracts, partitions, and its feed.
pub struct OptionsDayPlan {
    /// The session.
    pub session: ExchangeSession,
    /// Point-in-time contract snapshot for the session date.
    pub contracts: Vec<ContractRecord>,
    /// Located option partitions in deterministic order.
    pub files: Vec<FileQueryFile>,
    /// The bounded replay feed for the day.
    pub feed: LakeReplayFeed,
}

impl OptionsDayPlan {
    /// The selected OSI symbols in deterministic order.
    pub fn symbols(&self) -> Vec<String> {
        self.contracts
            .iter()
            .map(|record| record.symbol.clone())
            .collect()
    }
}

/// Stream deterministic option events one explicit session at a time.
pub struct OptionsReplayFeed {
    sessions: Vec<ExchangeSession>,
    universe: Rc<OptionUniverseView>,
    files: Rc<dyn OptionPartitionPort>,
    reader: Rc<dyn ParquetReader>,
    data_types: Vec<DataType>,
    streams: Vec<ReplayStream>,
    last_fingerprints: ReplayFingerprints,
}

impl OptionsReplayFeed {
    /// Build a day-bounded options replay feed.
    pub fn new(
        sessions: Vec<ExchangeSession>,
        universe: Rc<OptionUniverseView>,
        files: Rc<dyn OptionPartitionPort>,
        reader: Rc<dyn ParquetReader>,
        data_types: Vec<DataType>,
        streams: Vec<ReplayStream>,
    ) -> Result<Self, DataError> {
        let sessions = normalize_sessions(sessions)?;
        let mut data_types = data_types;
        data_types.sort_by(|a, b| a.value().cmp(b.value()));
        data_types.dedup();
        if data_types.is_empty() {
            return Err(DataError::value(
                "at least one option replay data type is required",
            ));
        }
        for data_type in &data_types {
            if !matches!(data_type, DataType::Quote | DataType::Trade) {
                return Err(DataError::value(
                    "option replay data types must be quote or trade",
                ));
            }
        }
        let mut feed = Self {
            sessions,
            universe,
            files,
            reader,
            data_types,
            streams,
            last_fingerprints: ReplayFingerprints {
                file: String::new(),
                row: String::new(),
                data: String::new(),
            },
        };
        feed.last_fingerprints = feed.baseline_fingerprints();
        Ok(feed)
    }

    /// The explicit replay sessions in chronological order.
    pub fn sessions(&self) -> &[ExchangeSession] {
        &self.sessions
    }

    /// The replayed option data types in deterministic order.
    pub fn data_types(&self) -> &[DataType] {
        &self.data_types
    }

    /// The fingerprints of the most recent completed run.
    pub fn fingerprints(&self) -> &ReplayFingerprints {
        &self.last_fingerprints
    }

    /// Plan one explicit session into contracts, files, and a feed.
    pub fn plan(&self, session: &ExchangeSession) -> Result<OptionsDayPlan, DataError> {
        if !self
            .sessions
            .iter()
            .any(|candidate| candidate.session_date == session.session_date)
        {
            return Err(DataError::value(
                "session is not part of this options replay",
            ));
        }
        let snapshot = self.universe.resolve(Some(session.open_time))?;
        let mut files = Vec::new();
        for symbol in snapshot.symbols() {
            for data_type in &self.data_types {
                if let Some(file) = self.files.file(session.session_date, *data_type, &symbol)? {
                    validate_file(&file, session, *data_type, &symbol)?;
                    files.push(file);
                }
            }
        }
        let feed = LakeReplayFeed::new(
            vec![*session],
            self.streams.clone(),
            files.clone(),
            Some(Rc::clone(&self.reader)),
        )?;
        Ok(OptionsDayPlan {
            session: *session,
            contracts: snapshot.contracts,
            files,
            feed,
        })
    }

    /// Plan every session lazily, in chronological order.
    ///
    /// The iterator resolves and opens one session at a time, so planning a
    /// long range never materializes more than that range's day plan in
    /// memory at once.
    pub fn days(&self) -> impl Iterator<Item = Result<OptionsDayPlan, DataError>> + '_ {
        self.sessions.iter().map(move |session| self.plan(session))
    }

    /// Stream the merged events of every session in chronological order.
    ///
    /// Each session is planned and drained before the next one is opened, so a
    /// multi-year run holds at most one day of event lookahead at a time.
    /// Fingerprints describe the most recent completed run once the stream is
    /// exhausted; a stream abandoned early leaves the previous run's
    /// fingerprints in place.
    pub fn stream(&mut self) -> impl Iterator<Item = Result<MarketEvent, DataError>> + '_ {
        EventStream::new(self)
    }

    /// Yield the merged events of every session in chronological order.
    pub fn events(&mut self) -> Result<Vec<MarketEvent>, DataError> {
        let mut emitted = Vec::new();
        for event in self.stream() {
            emitted.push(event?);
        }
        Ok(emitted)
    }

    fn header_frames(&self) -> Vec<Vec<u8>> {
        let spec = self.universe.spec();
        let mut static_symbols = spec.static_symbols.clone();
        static_symbols.insert(0, spec.mode.value().to_string());
        static_symbols.insert(0, spec.underlying.clone());
        static_symbols.insert(0, "fca-options-replay-run-v1".to_string());
        let data_types: Vec<String> = self
            .data_types
            .iter()
            .map(|data_type| data_type.value().to_string())
            .collect();
        let sessions: Vec<String> = self
            .sessions
            .iter()
            .map(|session| session.session_date.isoformat())
            .collect();
        vec![
            canonical_fields(&static_symbols),
            canonical_fields(&data_types),
            canonical_fields(&sessions),
        ]
    }

    fn baseline_fingerprints(&self) -> ReplayFingerprints {
        let header = self.header_frames();
        let mut digest = Sha256::new();
        for frame in &header {
            update_frame(&mut digest, frame);
        }
        let value = hex_string(&digest.finalize());
        ReplayFingerprints {
            file: value.clone(),
            row: value.clone(),
            data: value,
        }
    }
}

/// Day-bounded event stream backing [`OptionsReplayFeed::stream`].
struct EventStream<'a> {
    feed: &'a mut OptionsReplayFeed,
    session_index: usize,
    current: std::vec::IntoIter<MarketEvent>,
    file_digest: Sha256,
    row_digest: Sha256,
    data_digest: Sha256,
    finished: bool,
}

impl<'a> EventStream<'a> {
    fn new(feed: &'a mut OptionsReplayFeed) -> Self {
        let mut file_digest = Sha256::new();
        let mut row_digest = Sha256::new();
        let mut data_digest = Sha256::new();
        for frame in feed.header_frames() {
            update_frame(&mut file_digest, &frame);
            update_frame(&mut row_digest, &frame);
            update_frame(&mut data_digest, &frame);
        }
        Self {
            feed,
            session_index: 0,
            current: Vec::new().into_iter(),
            file_digest,
            row_digest,
            data_digest,
            finished: false,
        }
    }

    fn finish(&mut self) {
        self.finished = true;
        self.feed.last_fingerprints = ReplayFingerprints {
            file: hex_string(&self.file_digest.clone().finalize()),
            row: hex_string(&self.row_digest.clone().finalize()),
            data: hex_string(&self.data_digest.clone().finalize()),
        };
    }
}

impl Iterator for EventStream<'_> {
    type Item = Result<MarketEvent, DataError>;

    fn next(&mut self) -> Option<Self::Item> {
        if self.finished {
            return None;
        }
        loop {
            if let Some(event) = self.current.next() {
                return Some(Ok(event));
            }
            if self.session_index >= self.feed.sessions.len() {
                self.finish();
                return None;
            }
            let session = self.feed.sessions[self.session_index];
            self.session_index += 1;
            let mut plan = match self.feed.plan(&session) {
                Ok(plan) => plan,
                Err(error) => {
                    self.finished = true;
                    return Some(Err(error));
                }
            };
            let events = match plan.feed.events() {
                Ok(events) => events,
                Err(error) => {
                    self.finished = true;
                    return Some(Err(error));
                }
            };
            let fingerprints = plan.feed.fingerprints().clone();
            update_frame(
                &mut self.file_digest,
                &canonical_fields(&[
                    session.session_date.isoformat(),
                    fingerprints.file_fingerprint().to_string(),
                ]),
            );
            update_frame(
                &mut self.row_digest,
                &canonical_fields(&[
                    session.session_date.isoformat(),
                    fingerprints.row_fingerprint().to_string(),
                ]),
            );
            update_frame(
                &mut self.data_digest,
                &canonical_fields(&[
                    session.session_date.isoformat(),
                    fingerprints.data_fingerprint().to_string(),
                ]),
            );
            self.current = events.into_iter();
        }
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

fn validate_file(
    file: &FileQueryFile,
    session: &ExchangeSession,
    data_type: DataType,
    symbol: &str,
) -> Result<(), DataError> {
    if file.asset_class != AssetClass::Option {
        return Err(DataError::value(format!(
            "option replay file {:?} is not an option partition",
            file.object_path
        )));
    }
    if file.data_type != data_type {
        return Err(DataError::value(format!(
            "option replay file {:?} is a {} partition, not {}",
            file.object_path,
            file.data_type.value(),
            data_type.value()
        )));
    }
    if file.session_date != session.session_date {
        return Err(DataError::value(format!(
            "option replay file {:?} belongs to {}, not {}",
            file.object_path,
            file.session_date.isoformat(),
            session.session_date.isoformat()
        )));
    }
    if canonical_symbol(&file.symbol)? != canonical_symbol(symbol)? {
        return Err(DataError::value(format!(
            "option replay file {:?} belongs to {:?}, not {:?}",
            file.object_path, file.symbol, symbol
        )));
    }
    Ok(())
}

#[cfg(test)]
#[path = "../tests/unit/options_replay.rs"]
mod tests;
