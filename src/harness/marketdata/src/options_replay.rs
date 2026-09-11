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

    /// Plan every session in order.
    pub fn days(&self) -> Result<Vec<OptionsDayPlan>, DataError> {
        self.sessions
            .iter()
            .map(|session| self.plan(session))
            .collect()
    }

    /// Yield the merged events of every session in chronological order.
    pub fn events(&mut self) -> Result<Vec<MarketEvent>, DataError> {
        let header = self.header_frames();
        let mut file_digest = Sha256::new();
        let mut row_digest = Sha256::new();
        let mut data_digest = Sha256::new();
        for frame in &header {
            update_frame(&mut file_digest, frame);
            update_frame(&mut row_digest, frame);
            update_frame(&mut data_digest, frame);
        }
        let mut emitted = Vec::new();
        for index in 0..self.sessions.len() {
            let session = self.sessions[index];
            let mut plan = self.plan(&session)?;
            let events = plan.feed.events()?;
            let fingerprints = plan.feed.fingerprints().clone();
            update_frame(
                &mut file_digest,
                &canonical_fields(&[
                    session.session_date.isoformat(),
                    fingerprints.file_fingerprint().to_string(),
                ]),
            );
            emitted.extend(events);
            update_frame(
                &mut row_digest,
                &canonical_fields(&[
                    session.session_date.isoformat(),
                    fingerprints.row_fingerprint().to_string(),
                ]),
            );
            update_frame(
                &mut data_digest,
                &canonical_fields(&[
                    session.session_date.isoformat(),
                    fingerprints.data_fingerprint().to_string(),
                ]),
            );
        }
        self.last_fingerprints = ReplayFingerprints {
            file: hex_string(&file_digest.finalize()),
            row: hex_string(&row_digest.finalize()),
            data: hex_string(&data_digest.finalize()),
        };
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
mod tests {
    use super::*;
    use crate::catalog::{MemoryContractsCatalog, OptionType};
    use crate::clock::ManualClock;
    use crate::events::TradeEvent;
    use crate::lake::partition_path;
    use crate::options_universe::{OptionUniverseSpec, SelectionMode};
    use crate::replay::StaticReader;
    use crate::time::DateTime;
    use num::Decimal18;
    use std::str::FromStr;

    fn date(year: i32, month: u32, day: u32) -> Date {
        Date::from_ymd(year, month, day).unwrap()
    }

    fn session(day: Date) -> ExchangeSession {
        ExchangeSession {
            session_date: day,
            open_time: DateTime::midnight(day),
            close_time: DateTime::midnight(day.add_days(1)),
        }
    }

    #[test]
    fn options_replay_plans_selected_contracts() {
        let day = date(2024, 1, 2);
        let contract = crate::catalog::ContractRecord::new(
            "AAPL240119C00150000",
            "AAPL",
            date(2024, 1, 19),
            Decimal18::from_str("150").unwrap(),
            OptionType::Call,
            100,
            None,
            None,
        )
        .unwrap();
        let catalog = Rc::new(MemoryContractsCatalog {
            contracts: vec![contract.clone()],
            front_sets: Vec::new(),
        });
        let spec = OptionUniverseSpec::new(
            "AAPL",
            SelectionMode::StaticOsi,
            vec!["AAPL240119C00150000".to_string()],
            None,
            None,
            None,
        )
        .unwrap();
        let universe = Rc::new(
            OptionUniverseView::new(
                Box::new(ManualClock::new(DateTime::midnight(day))),
                catalog,
                spec,
                None,
            )
            .unwrap(),
        );
        let file = FileQueryFile {
            asset_class: AssetClass::Option,
            data_type: DataType::Trade,
            symbol: "AAPL240119C00150000".to_string(),
            session_date: day,
            object_path: partition_path(
                day,
                AssetClass::Option,
                DataType::Trade,
                "AAPL240119C00150000",
            )
            .unwrap(),
            read_url: "https://example.invalid/option".to_string(),
            byte_size: 1,
            row_count: 1,
        };
        let port = Rc::new(MemoryOptionPartitionPort { files: vec![file] });
        let reader = Rc::new(StaticReader {
            events: vec![MarketEvent::Trade(TradeEvent::new(
                "AAPL240119C00150000".to_string(),
                DateTime::midnight(day),
                1,
                Decimal18::from_str("1").unwrap(),
                Decimal18::from_str("1").unwrap(),
            ))],
        });
        let mut feed = OptionsReplayFeed::new(
            vec![session(day)],
            universe,
            port,
            reader,
            vec![DataType::Trade],
            Vec::new(),
        )
        .unwrap();
        let plan = feed.plan(&session(day)).unwrap();
        assert_eq!(plan.symbols(), vec!["AAPL240119C00150000"]);
        let events = feed.events().unwrap();
        assert_eq!(events.len(), 1);
        assert!(!feed.fingerprints().data.is_empty());
    }
}
