//! Owns lake snapshot planning, bounded cursor threads and chronological merging.
//! The engine receives only a re-iterable replay factory, never a lake client.
use backtest::backtest::fixture::DataPreflight;
use backtest::backtest::fixture::ExchangeSession;
use backtest::backtest::fixture::MarketEvent;
use backtest::backtest::fixture::ReplayFingerprints;
use backtest::backtest::stream::Events;
use backtest::backtest::stream::Factory;
use backtest::backtest::stream::PlannedReplay;
use backtest::backtest::stream::StreamedReplay;
use harness_contracts::sha256::digest_hex;
use harness_lakeclient::BlockingLakeClient;
use harness_lakeclient::LakeClientError;
use harness_lakeclient::ReadSpec;
use harness_trading::replay_errors::BacktestError;
use marketdata::lake::DataType;
use std::collections::BTreeSet;
use std::sync::mpsc;
use std::sync::Arc;
/// Committed catalog manifests establish availability and immutable provenance.
/// Only the small bar stream is opened to assemble completed consumer frames.
pub fn catalog(
    client: Arc<BlockingLakeClient>,
    inputs: Vec<LakeInput>,
    source_id: String,
    start: i64,
    end: i64,
    reporter: Option<harness_contracts::progress::ProgressReporter>,
) -> Result<PlannedReplay, BacktestError> {
    if inputs.len() > 32 {
        return Err(BacktestError::DataPreflight(
            "replay supports at most 32 concurrent symbol/type inputs".into(),
        ));
    }
    let fail = |e: harness_lakeclient::LakeClientError| BacktestError::DataPreflight(e.to_string());
    let mut coverage = BTreeSet::new();
    let mut days = BTreeSet::new();
    let mut symbols = BTreeSet::new();
    let mut counts = [0usize; 3];
    let mut file_count = 0usize;
    let mut pins = Vec::new();
    for input in &inputs {
        let snapshot = client.snapshot(&input.read).map_err(fail)?;
        snapshot
            .validate()
            .map_err(|e| BacktestError::DataPreflight(e.to_string()))?;
        let count = snapshot
            .files
            .iter()
            .try_fold(0usize, |total, p| {
                usize::try_from(p.row_count)
                    .ok()
                    .and_then(|n| total.checked_add(n))
            })
            .ok_or_else(|| BacktestError::DataPreflight("catalog row count overflows".into()))?;
        if count > 0 {
            coverage.insert((input.data_type.value().into(), input.symbol.clone()));
            symbols.insert(input.symbol.clone());
        }
        for file in &snapshot.files {
            if file.row_count > 0 {
                days.insert(file.session_date.clone());
            }
        }
        let kind = match input.data_type {
            DataType::Quote => 0,
            DataType::Trade => 1,
            DataType::Bars => 2,
            _ => {
                return Err(BacktestError::DataPreflight(
                    "reference data cannot be replayed".into(),
                ))
            }
        };
        counts[kind] = counts[kind]
            .checked_add(count)
            .ok_or_else(|| BacktestError::DataPreflight("catalog row count overflows".into()))?;
        file_count += snapshot.files.len();
        pins.push((input.symbol.clone(), snapshot));
    }
    let file = digest_hex(
        &serde_json::to_vec(&pins).map_err(|e| BacktestError::DataPreflight(e.to_string()))?,
    );
    let data = digest_hex(format!("fca:harness-catalog-replay\0{start}\0{end}\0{file}").as_bytes());
    let event_count = counts
        .iter()
        .try_fold(0usize, |sum, n| sum.checked_add(*n))
        .ok_or_else(|| BacktestError::DataPreflight("catalog row count overflows".into()))?;
    let sessions: Vec<_> = days
        .into_iter()
        .map(|text| {
            let day = marketdata::Date::parse(&text)
                .expect("validated catalog date")
                .days() as i64;
            ExchangeSession {
                session_date_days: day,
                open_ns: day * 86_400_000_000_000,
                close_ns: (day + 1) * 86_400_000_000_000,
            }
        })
        .collect();
    let evidence = DataPreflight {
        source_id,
        session_count: sessions.len(),
        event_count,
        quote_count: counts[0],
        trade_count: counts[1],
        bar_count: counts[2],
        file_count,
        symbols: symbols.into_iter().collect(),
        fingerprints: ReplayFingerprints {
            file,
            row: None,
            data,
        },
    };
    let bar_inputs = inputs
        .iter()
        .filter(|input| input.data_type == DataType::Bars)
        .cloned()
        .collect::<Vec<_>>();
    let bars = if bar_inputs.is_empty() {
        Vec::new()
    } else {
        lake_factory(client.clone(), bar_inputs, start, end)()?.collect::<Result<Vec<_>, _>>()?
    };
    let expected_count = (start.rem_euclid(86_400_000_000_000) == 0
        && end.rem_euclid(86_400_000_000_000) == 0)
        .then_some(event_count);
    let factory = lake_factory(client, inputs, start, end);
    Ok((
        Arc::new(StreamedReplay::new(
            factory,
            evidence,
            coverage,
            expected_count,
            reporter,
        )),
        bars,
        sessions,
    ))
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
                        let decoded = marketdata::map_arrow_events(
                            input.data_type,
                            &input.lake_symbol,
                            std::slice::from_ref(&batch),
                            |event| crate::acquisition::lake_event(event, &input.symbol),
                        )
                        .map_err(|e| LakeClientError::Decode(e.to_string()))?;
                        let nanos = batch.column_by_name("timestamp").and_then(|c| {
                            c.as_any()
                                .downcast_ref::<arrow_array::TimestampNanosecondArray>()
                        });
                        for (index, mut event) in decoded.into_iter().enumerate() {
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
            heads: (0..inputs.len()).map(|_| None).collect(),
            winners: [usize::MAX; 64],
            width: inputs.len().next_power_of_two(),
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
    heads: Vec<Option<Head>>,
    // At most 32 inputs. A fixed tournament updates only the winner's path;
    // no tree node is allocated or moved for each incoming market event.
    winners: [usize; 64],
    width: usize,
    failed: bool,
}
struct Head {
    key: (i64, u64, u8, usize),
    event: MarketEvent,
}
impl Merge {
    fn advance(&mut self, index: usize) -> Result<(), BacktestError> {
        self.heads[index] = self.cursors[index].next()?.map(|event| {
            let (t, s, c) = key(&event);
            Head { key: (t, s, c, index), event }
        });
        let mut node = self.width + index;
        self.winners[node] = if self.heads[index].is_some() { index } else { usize::MAX };
        while node > 1 {
            node /= 2;
            let left = self.winners[node * 2];
            let right = self.winners[node * 2 + 1];
            self.winners[node] = if left == usize::MAX {
                right
            } else if right == usize::MAX || self.heads[left].as_ref().unwrap().key <= self.heads[right].as_ref().unwrap().key {
                left
            } else {
                right
            };
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
        let index = self.winners[1];
        if index == usize::MAX {
            return None;
        }
        let event = self.heads[index].take().unwrap().event;
        if let Err(error) = self.advance(index) {
            self.failed = true;
            return Some(Err(error));
        }
        Some(Ok(event))
    }
}
