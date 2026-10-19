//! Re-iterable, bounded chronological replay over pinned market-data snapshots.
//!
//! At most one decoded batch and one same-time group per input are retained.
//! Dropping the consumer closes its rendezvous channels and stops producers.

use super::{
    errors::BacktestError,
    fixture::{DataPreflight, ExchangeSession, MarketEvent, ReplayFingerprints},
};
use crate::sha256::{digest_hex, Sha256};
use harness_lakeclient::{BlockingLakeClient, LakeClientError, ReadSpec};
use marketdata::lake::DataType;
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::{mpsc, Arc},
};

pub type Events = Box<dyn Iterator<Item = Result<MarketEvent, BacktestError>> + Send>;
type Factory = Arc<dyn Fn() -> Result<Events, BacktestError> + Send + Sync>;
pub type ScannedReplay = (Arc<StreamedReplay>, Vec<MarketEvent>, Vec<ExchangeSession>);

/// Immutable replay factory and evidence collected without retaining tick rows.
pub struct StreamedReplay {
    factory: Factory,
    pub evidence: DataPreflight,
    pub reporter: Option<harness_contracts::progress::ProgressReporter>,
    coverage: BTreeSet<(String, String)>,
    pub bounds: Option<(i64, i64)>,
}
impl std::fmt::Debug for StreamedReplay {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("StreamedReplay")
            .field("evidence", &self.evidence)
            .finish()
    }
}
impl PartialEq for StreamedReplay {
    fn eq(&self, other: &Self) -> bool {
        self.evidence == other.evidence
    }
}
impl Eq for StreamedReplay {}
impl StreamedReplay {
    pub fn open(&self) -> Result<Events, BacktestError> {
        (self.factory)()
    }
    pub fn covers(&self, kind: &str, symbol: &str) -> bool {
        self.coverage.contains(&(kind.into(), symbol.into()))
    }

    /// Scan once for preflight and row fingerprints, retaining only source bars.
    pub fn scan(
        factory: Factory,
        source_id: String,
        start: i64,
        end: i64,
        reporter: Option<harness_contracts::progress::ProgressReporter>,
    ) -> Result<ScannedReplay, BacktestError> {
        let mut coverage = BTreeSet::new();
        let mut days = BTreeSet::new();
        let mut symbols = BTreeSet::new();
        let mut bars = Vec::new();
        let (mut quote_count, mut trade_count, mut bar_count) = (0, 0, 0);
        let mut rows = Sha256::new();
        rows.update(b"fca:harness-replay-rows:v1\0");
        let mut last_report = std::time::Instant::now();
        let mut bounds: Option<(i64, i64)> = None;
        for event in factory()? {
            let event = event?;
            if event.timestamp_ns() < start || event.timestamp_ns() >= end {
                return Err(BacktestError::DataPreflight(
                    "stream event outside accepted run window".into(),
                ));
            }
            days.insert(event.timestamp_ns().div_euclid(86_400_000_000_000));
            bounds = Some(bounds.map_or(
                (event.timestamp_ns(), event.timestamp_ns()),
                |(first, last)| {
                    (
                        first.min(event.timestamp_ns()),
                        last.max(event.timestamp_ns()),
                    )
                },
            ));
            symbols.insert(event.symbol().to_string());
            let kind = match &event {
                MarketEvent::Quote { .. } => {
                    quote_count += 1;
                    "quote"
                }
                MarketEvent::Trade { .. } => {
                    trade_count += 1;
                    "trade"
                }
                MarketEvent::Bar { .. } => {
                    bar_count += 1;
                    "bars"
                }
            };
            coverage.insert((kind.into(), event.symbol().to_string()));
            rows.update(event.to_json().to_canonical_string().as_bytes());
            rows.update(b"\n");
            if matches!(event, MarketEvent::Bar { .. }) {
                bars.push(event);
            }
            if (quote_count + trade_count + bar_count) % 8192 == 0
                && last_report.elapsed() >= std::time::Duration::from_secs(1)
            {
                if let Some(report) = &reporter {
                    let mut progress = harness_contracts::progress::ExecutionProgress::stage(
                        harness_contracts::progress::ExecutionStage::ReadingData,
                    );
                    progress.rows = (quote_count + trade_count + bar_count) as u64;
                    report(progress).map_err(BacktestError::DataPreflight)?;
                }
                last_report = std::time::Instant::now();
            }
        }
        let file = {
            let mut hash = Sha256::new();
            hash.update(b"fca:harness-replay-files:v1\0");
            hash.finalize_hex()
        };
        let row = rows.finalize_hex();
        let data = digest_hex(format!("fca:harness-replay-data:v1\0{file}\0{row}").as_bytes());
        let sessions = days
            .into_iter()
            .map(|day| ExchangeSession {
                session_date_days: day,
                open_ns: day * 86_400_000_000_000,
                close_ns: (day + 1) * 86_400_000_000_000,
            })
            .collect::<Vec<_>>();
        let evidence = DataPreflight {
            source_id,
            session_count: sessions.len(),
            event_count: quote_count + trade_count + bar_count,
            quote_count,
            trade_count,
            bar_count,
            file_count: 0,
            symbols: symbols.into_iter().collect(),
            fingerprints: ReplayFingerprints { file, row, data },
        };
        if let Some(report) = &reporter {
            let mut progress = harness_contracts::progress::ExecutionProgress::stage(
                harness_contracts::progress::ExecutionStage::ReadingData,
            );
            progress.rows = evidence.event_count as u64;
            progress.completed = true;
            report(progress).map_err(BacktestError::DataPreflight)?;
        }
        Ok((
            Arc::new(Self {
                factory,
                evidence,
                coverage,
                reporter,
                bounds,
            }),
            bars,
            sessions,
        ))
    }
}

/// One accepted input; snapshot identity remains pinned in the shared client.
#[derive(Clone)]
pub struct LakeInput {
    pub symbol: String,
    pub lake_symbol: String,
    pub data_type: DataType,
    pub read: ReadSpec,
}

pub fn lake_factory(
    client: Arc<BlockingLakeClient>,
    inputs: Vec<LakeInput>,
    start: i64,
    end: i64,
) -> Factory {
    Arc::new(move || {
        // An explicit input cap bounds cursor threads and retained decoded heads.
        if inputs.len() > 32 {
            return Err(BacktestError::DataPreflight(
                "replay supports at most 32 concurrent symbol/type inputs".into(),
            ));
        }
        let mut cursors = Vec::new();
        for input in &inputs {
            let client = client
                .replay_cursor()
                .map_err(|e| BacktestError::DataPreflight(e.to_string()))?;
            let input = input.clone();
            let (sender, receiver) = mpsc::sync_channel(0);
            std::thread::spawn(move || {
                let mut group: Vec<MarketEvent> = Vec::new();
                let mut ready = Vec::new();
                let send_group = |group: &mut Vec<MarketEvent>,
                                  ready: &mut Vec<MarketEvent>|
                 -> Result<(), LakeClientError> {
                    group.sort_by_key(key);
                    ready.append(group);
                    if ready.len() >= 8192 {
                        sender.send(Ok(std::mem::take(ready))).map_err(|_| {
                            LakeClientError::Decode("replay consumer closed".into())
                        })?;
                    }
                    Ok(())
                };
                let result = client
                    .visit_arrow(&input.read, |batch| {
                        let decoded = marketdata::events_from_arrow(
                            input.data_type,
                            &input.lake_symbol,
                            std::slice::from_ref(&batch),
                        )
                        .map_err(|e| LakeClientError::Decode(e.to_string()))?;
                        let nanos = batch.column_by_name("timestamp").and_then(|c| {
                            c.as_any()
                                .downcast_ref::<arrow_array::TimestampNanosecondArray>()
                        });
                        for (index, event) in decoded.iter().enumerate() {
                            let mut event = super::job::lake_event(event, &input.symbol);
                            if let Some(nanos) = nanos {
                                match &mut event {
                                    MarketEvent::Quote { timestamp_ns, .. }
                                    | MarketEvent::Trade { timestamp_ns, .. }
                                    | MarketEvent::Bar { timestamp_ns, .. } => {
                                        *timestamp_ns = nanos.value(index)
                                    }
                                }
                            }
                            if event.timestamp_ns() < start || event.timestamp_ns() >= end {
                                continue;
                            }
                            if let Some(previous) = group.last() {
                                if previous.timestamp_ns() > event.timestamp_ns() {
                                    return Err(LakeClientError::Decode(
                                        "pinned source timestamps regress".into(),
                                    ));
                                }
                                if previous.timestamp_ns() != event.timestamp_ns() {
                                    send_group(&mut group, &mut ready)?;
                                }
                            }
                            if group.len() >= 65_536 {
                                return Err(LakeClientError::Decode(
                                    "same-time replay group exceeds bounded capacity".into(),
                                ));
                            }
                            group.push(event);
                        }
                        Ok(())
                    })
                    .and_then(|_| {
                        send_group(&mut group, &mut ready)?;
                        if !ready.is_empty() {
                            sender.send(Ok(ready)).map_err(|_| {
                                LakeClientError::Decode("replay consumer closed".into())
                            })?;
                        }
                        sender
                            .send(Ok(Vec::new()))
                            .map_err(|_| LakeClientError::Decode("replay consumer closed".into()))
                    });
                if let Err(error) = result {
                    let _ = sender.send(Err(BacktestError::DataPreflight(error.to_string())));
                }
            });
            cursors.push(Cursor {
                receiver,
                batch: Vec::new().into_iter(),
                finished: false,
            });
        }
        let mut merged = Merge {
            cursors,
            heads: BTreeMap::new(),
            failed: false,
        };
        for index in 0..merged.cursors.len() {
            merged.advance(index)?;
        }
        Ok(Box::new(merged) as Events)
    })
}

struct Cursor {
    receiver: mpsc::Receiver<Result<Vec<MarketEvent>, BacktestError>>,
    batch: std::vec::IntoIter<MarketEvent>,
    finished: bool,
}
impl Cursor {
    fn next(&mut self) -> Result<Option<MarketEvent>, BacktestError> {
        loop {
            if self.finished {
                return Ok(None);
            }
            if let Some(event) = self.batch.next() {
                return Ok(Some(event));
            }
            match self.receiver.recv() {
                Ok(batch) => {
                    let batch = batch?;
                    if batch.is_empty() {
                        self.finished = true;
                    }
                    self.batch = batch.into_iter();
                }
                Err(_) => {
                    return Err(BacktestError::DataPreflight(
                        "replay producer terminated before completion".into(),
                    ))
                }
            }
        }
    }
}
pub fn key(event: &MarketEvent) -> (i64, u64, u8) {
    (
        event.timestamp_ns(),
        event.sequence(),
        match event {
            MarketEvent::Quote { .. } => 0,
            MarketEvent::Trade { .. } => 1,
            MarketEvent::Bar { .. } => 2,
        },
    )
}
struct Merge {
    cursors: Vec<Cursor>,
    heads: BTreeMap<(i64, u64, u8, usize), MarketEvent>,
    failed: bool,
}
impl Merge {
    fn advance(&mut self, index: usize) -> Result<(), BacktestError> {
        if let Some(event) = self.cursors[index].next()? {
            let (t, s, c) = key(&event);
            self.heads.insert((t, s, c, index), event);
        }
        Ok(())
    }
}
impl Iterator for Merge {
    type Item = Result<MarketEvent, BacktestError>;
    fn next(&mut self) -> Option<Self::Item> {
        if self.failed {
            return None;
        }
        let ((_, _, _, index), event) = self.heads.pop_first()?;
        if let Err(error) = self.advance(index) {
            self.failed = true;
            return Some(Err(error));
        }
        Some(Ok(event))
    }
}
