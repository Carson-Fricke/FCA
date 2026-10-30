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
        lake_factory_with_cache(
            client.clone(),
            inputs.clone(),
            start,
            end,
            crate::prepared_replay::PreparedReplayConfig::from_env()?,
        )()
    })
}

/// Explicit storage composition, also used by isolated replay behavior checks.
pub fn lake_factory_with_cache(
    client: Arc<BlockingLakeClient>,
    inputs: Vec<LakeInput>,
    start: i64,
    end: i64,
    cache_config: crate::prepared_replay::PreparedReplayConfig,
) -> Factory {
    Arc::new(move || {
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
            let cache = crate::prepared_replay::Cache::new(cache_config.clone());
            let (sender, receiver) = mpsc::sync_channel(0);
            std::thread::spawn(move || {
                let result = produce(&client, &input, start, end, &sender, &cache);
                if let Err(error) = result {
                    let _ = sender.send(Err(error));
                }
            });
            cursors.push(Cursor {
                receiver,
                batch: Vec::new().into_iter(),
                block: None,
                index: 0,
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

enum Batch {
    Events(Vec<MarketEvent>),
    Ticks(Arc<harness_trading::market::TickBlock>),
    End,
}
fn produce(
    client: &BlockingLakeClient,
    input: &LakeInput,
    start: i64,
    end: i64,
    sender: &mpsc::SyncSender<Result<Batch, BacktestError>>,
    cache: &crate::prepared_replay::Cache,
) -> Result<(), BacktestError> {
    let fail = |e: LakeClientError| BacktestError::DataPreflight(e.to_string());
    let send = |batch| {
        sender
            .send(Ok(batch))
            .map_err(|_| BacktestError::DataPreflight("replay consumer closed".into()))
    };
    let ticks = matches!(input.data_type, DataType::Quote | DataType::Trade);
    let key = if ticks {
        let snapshot = client.snapshot(&input.read).map_err(fail)?;
        snapshot
            .validate()
            .map_err(|e| BacktestError::DataPreflight(e.to_string()))?;
        cache.key(
            &serde_json::to_vec(&(snapshot, &input.symbol, start, end))
                .map_err(|e| BacktestError::DataPreflight(e.to_string()))?,
        )
    } else {
        String::new()
    };
    let mut previous = None;
    if ticks {
        if let Some(reader) = cache.open(&key)? {
            for block in reader {
                let block = block?;
                let kind = if input.data_type == DataType::Quote {
                    harness_trading::market::TickKind::Quote
                } else {
                    harness_trading::market::TickKind::Trade
                };
                if block.symbol() != input.symbol
                    || block.kind() != kind
                    || block
                        .rows()
                        .iter()
                        .any(|r| r.timestamp_ns < start || r.timestamp_ns >= end)
                    || previous.is_some_and(|key| {
                        key > (block.rows()[0].timestamp_ns, block.rows()[0].sequence)
                    })
                {
                    return Err(BacktestError::DataPreflight(
                        "prepared replay does not match accepted input/order".into(),
                    ));
                }
                let last = block.rows().last().expect("validated nonempty block");
                previous = Some((last.timestamp_ns, last.sequence));
                send(Batch::Ticks(block))?;
            }
            return send(Batch::End);
        }
    }
    let mut writer = if ticks { cache.writer(&key)? } else { None };
    let mut group: Vec<MarketEvent> = Vec::new();
    let mut ready = Vec::new();
    let mut flush = |ready: &mut Vec<MarketEvent>| -> Result<(), LakeClientError> {
        if ready.is_empty() {
            return Ok(());
        }
        let batch = if ticks {
            let block = pack(input, std::mem::take(ready))
                .map_err(|e| LakeClientError::Decode(e.to_string()))?;
            if let Some(writer) = &mut writer {
                writer
                    .push(&block)
                    .map_err(|e| LakeClientError::Decode(e.to_string()))?;
            }
            Batch::Ticks(block)
        } else {
            Batch::Events(std::mem::take(ready))
        };
        send(batch).map_err(|e| LakeClientError::Decode(e.to_string()))
    };
    client
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
                        MarketEvent::Block(_) => {
                            unreachable!("Arrow decoder returns owned source rows")
                        }
                    }
                }
                if event.timestamp_ns() < start || event.timestamp_ns() >= end {
                    continue;
                }
                if let Some(prior) = group.last() {
                    if prior.timestamp_ns() > event.timestamp_ns() {
                        return Err(LakeClientError::Decode(
                            "pinned source timestamps regress".into(),
                        ));
                    }
                    if prior.timestamp_ns() != event.timestamp_ns() {
                        group.sort_by_key(key_of);
                        ready.append(&mut group);
                        if ready.len() >= 8192 {
                            flush(&mut ready)?;
                        }
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
        .map_err(fail)?;
    group.sort_by_key(key_of);
    ready.append(&mut group);
    flush(&mut ready).map_err(fail)?;
    if let Some(writer) = writer {
        writer.finish()?;
    }
    send(Batch::End)
}

fn pack(
    input: &LakeInput,
    events: Vec<MarketEvent>,
) -> Result<Arc<harness_trading::market::TickBlock>, BacktestError> {
    use harness_trading::market::{TickBlock, TickKind, TickRow};
    let kind = if input.data_type == DataType::Quote {
        TickKind::Quote
    } else {
        TickKind::Trade
    };
    let mut text = String::new();
    let mut venues = std::collections::BTreeMap::new();
    let mut rows = Vec::with_capacity(events.len());
    let append = |text: &mut String, value: &str| -> Result<(u32, u32), BacktestError> {
        if text.len().saturating_add(value.len()) > harness_trading::market::MAX_BLOCK_TEXT_BYTES {
            return Err(BacktestError::DataPreflight(
                "tick block text exceeds bounded capacity".into(),
            ));
        }
        let result = (text.len() as u32, value.len() as u32);
        text.push_str(value);
        Ok(result)
    };
    for event in events {
        let (timestamp_ns, sequence, event_id, venue, trade_id, values) = match event {
            MarketEvent::Quote {
                timestamp_ns,
                sequence,
                event_id,
                venue,
                bid,
                ask,
                bid_quantity,
                ask_quantity,
                ..
            } => (
                timestamp_ns,
                sequence,
                event_id,
                venue,
                String::new(),
                [bid, ask, bid_quantity, ask_quantity],
            ),
            MarketEvent::Trade {
                timestamp_ns,
                sequence,
                event_id,
                venue,
                trade_id,
                price,
                quantity,
                ..
            } => (
                timestamp_ns,
                sequence,
                event_id,
                venue,
                trade_id,
                [price, quantity, num::Decimal18::ZERO, num::Decimal18::ZERO],
            ),
            _ => {
                return Err(BacktestError::DataPreflight(
                    "non-tick in compact tick block".into(),
                ))
            }
        };
        let venue_range = if let Some(range) = venues.get(&venue) {
            *range
        } else {
            let range = append(&mut text, &venue)?;
            venues.insert(venue, range);
            range
        };
        rows.push(TickRow {
            timestamp_ns,
            sequence,
            values,
            event_id: append(&mut text, &event_id)?,
            venue: venue_range,
            trade_id: append(&mut text, &trade_id)?,
        });
    }
    TickBlock::new(input.symbol.clone(), kind, rows, text)
        .map(Arc::new)
        .map_err(BacktestError::DataPreflight)
}
fn key_of(event: &MarketEvent) -> (i64, u64, u8) {
    key(event)
}
struct Cursor {
    receiver: mpsc::Receiver<Result<Batch, BacktestError>>,
    batch: std::vec::IntoIter<MarketEvent>,
    block: Option<Arc<harness_trading::market::TickBlock>>,
    index: usize,
    finished: bool,
}
impl Cursor {
    fn next(&mut self) -> Result<Option<MarketEvent>, BacktestError> {
        loop {
            if self.finished {
                return Ok(None);
            }
            if let Some(block) = &self.block {
                if let Some(event) = block.event(self.index) {
                    self.index += 1;
                    return Ok(Some(MarketEvent::Block(event)));
                }
                self.block = None;
            }
            if let Some(event) = self.batch.next() {
                return Ok(Some(event));
            }
            match self.receiver.recv() {
                Ok(Ok(Batch::Ticks(block))) => {
                    self.block = Some(block);
                    self.index = 0;
                }
                Ok(Ok(Batch::Events(events))) => self.batch = events.into_iter(),
                Ok(Ok(Batch::End)) => self.finished = true,
                Ok(Err(error)) => return Err(error),
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
    (event.timestamp_ns(), event.sequence(), event.kind() as u8)
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
            Head {
                key: (t, s, c, index),
                event,
            }
        });
        let mut node = self.width + index;
        self.winners[node] = if self.heads[index].is_some() {
            index
        } else {
            usize::MAX
        };
        while node > 1 {
            node /= 2;
            let left = self.winners[node * 2];
            let right = self.winners[node * 2 + 1];
            self.winners[node] = if left == usize::MAX {
                right
            } else if right == usize::MAX
                || self.heads[left].as_ref().unwrap().key <= self.heads[right].as_ref().unwrap().key
            {
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
