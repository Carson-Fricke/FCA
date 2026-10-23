//! Owns deterministic dispatch ordering and its hash; no venue or strategy policy.
use harness_contracts::canonical_json::JsonValue;
use harness_contracts::sha256::Sha256;
use harness_trading::program::*;
use std::cmp::Ordering;
use std::collections::BinaryHeap;

/// One bus event.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum EngineEvent {
    /// A market event.
    Market(EngineMarket),
    /// One atomic study frame, scheduled at availability.
    BarFrame(EngineBarFrame),
    /// An execution report.
    Report(EngineReport),
    /// A fill.
    Fill(EngineFill),
}

impl EngineEvent {
    /// Event time in epoch nanoseconds.
    #[must_use]
    pub const fn timestamp_ns(&self) -> i64 {
        match self {
            Self::Market(market) => market.timestamp_ns(),
            Self::BarFrame(frame) => frame.available_at_ns,
            Self::Report(report) => report.timestamp_ns,
            Self::Fill(fill) => fill.timestamp_ns,
        }
    }

    /// Event sequence.
    #[must_use]
    pub const fn sequence(&self) -> u64 {
        match self {
            Self::Market(market) => market.sequence(),
            Self::BarFrame(_) => 0,
            Self::Report(report) => report.sequence,
            Self::Fill(fill) => fill.sequence,
        }
    }

    /// Event identifier.
    #[must_use]
    pub fn event_id(&self) -> &str {
        match self {
            Self::Market(market) => market.event_id(),
            Self::BarFrame(_) => "study-bar-frame",
            Self::Report(report) => &report.event_id,
            Self::Fill(fill) => &fill.event_id,
        }
    }

    /// Class priority used by the queue, mirroring `EVENT_CLASS_PRIORITY`.
    ///
    /// Frames carry the bar class priority at their availability time.
    #[must_use]
    pub const fn class_priority(&self) -> u8 {
        match self {
            Self::Market(EngineMarket::Quote { .. }) => 0,
            Self::Market(EngineMarket::Trade { .. }) => 1,
            Self::Market(EngineMarket::Bar(_)) => 9,
            Self::BarFrame(_) => 9,
            Self::Report(_) => 4,
            Self::Fill(_) => 5,
        }
    }

    /// Python `module.qualname` for the dispatch identity.
    #[must_use]
    pub const fn qualname(&self) -> &'static str {
        match self {
            Self::Market(EngineMarket::Quote { .. }) => "harness.core.types.events.QuoteEvent",
            Self::Market(EngineMarket::Trade { .. }) => "harness.core.types.events.TradeEvent",
            Self::Market(EngineMarket::Bar(_)) => "harness.core.types.events.BarEvent",
            Self::BarFrame(_) => "harness.backtest.EngineBarFrame",
            Self::Report(_) => "harness.core.types.accounting.ExecutionReport",
            Self::Fill(_) => "harness.core.types.accounting.Fill",
        }
    }

    /// Python `event_type.value` for the dispatch identity.
    #[must_use]
    pub const fn event_type(&self) -> &'static str {
        match self {
            Self::Market(EngineMarket::Quote { .. }) => "quote",
            Self::Market(EngineMarket::Trade { .. }) => "trade",
            Self::Market(EngineMarket::Bar(_)) => "bar",
            Self::BarFrame(_) => "bar_frame",
            Self::Report(_) => "execution_report",
            Self::Fill(_) => "fill",
        }
    }
}

/// One queue entry ordered by time, sequence, class, then feed order.
#[derive(Clone, Debug)]
pub(super) struct QueuedEvent {
    pub(super) event: EngineEvent,
    feed_ordinal: u64,
    pub(super) phase: u8,
}

impl PartialEq for QueuedEvent {
    fn eq(&self, other: &Self) -> bool {
        self.key() == other.key()
    }
}
impl Eq for QueuedEvent {}
impl PartialOrd for QueuedEvent {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}
impl Ord for QueuedEvent {
    fn cmp(&self, other: &Self) -> Ordering {
        // `BinaryHeap` is a max-heap; reverse so the smallest key pops first.
        other.key().cmp(&self.key())
    }
}

impl QueuedEvent {
    fn key(&self) -> (i64, u8, u64, u8, u64) {
        (
            self.event.timestamp_ns(),
            self.phase,
            self.event.sequence(),
            self.event.class_priority(),
            self.feed_ordinal,
        )
    }
}

/// The deterministic event bus and its Python-compatible dispatch digest.
#[derive(Debug, Default)]
pub struct EventBus {
    pub(super) heap: BinaryHeap<QueuedEvent>,
    feed_ordinal: u64,
    hasher: Sha256,
    dispatched_count: u64,
    pub(super) frame_replay: bool,
}

impl EventBus {
    /// Create an empty bus.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Enqueue one event.
    pub fn schedule(&mut self, event: EngineEvent) {
        // Frame-enabled replay observes every same-time tick before frames,
        // then dispatches generated execution responses. Legacy ticks keep their key.
        let phase = if self.frame_replay {
            match &event {
                EngineEvent::Market(_) => 0,
                EngineEvent::BarFrame(_) => 1,
                EngineEvent::Report(_) | EngineEvent::Fill(_) => 2,
            }
        } else {
            0
        };
        self.heap.push(QueuedEvent {
            event,
            feed_ordinal: self.feed_ordinal,
            phase,
        });
        self.feed_ordinal += 1;
    }

    /// Pop and record the next event, or `None` when empty.
    pub fn dispatch_next(&mut self) -> Option<EngineEvent> {
        let queued = self.heap.pop()?;
        self.record_dispatch(&queued.event);
        Some(queued.event)
    }

    /// The stable SHA-256 digest of dispatched event identities.
    #[must_use]
    pub fn dispatch_hash(&self) -> String {
        self.hasher.clone().finalize_hex()
    }

    /// The number of events dispatched.
    #[must_use]
    pub const fn dispatched_count(&self) -> u64 {
        self.dispatched_count
    }

    fn record_dispatch(&mut self, event: &EngineEvent) {
        let identity = format!(
            "{}\u{0}{}\u{0}{}\u{0}{}\u{0}{}",
            event.qualname(),
            event.event_type(),
            crate::backtest::manifest::utc_timestamp(event.timestamp_ns(), false),
            event.sequence(),
            event.event_id(),
        );
        let bytes = identity.as_bytes();
        self.hasher.update(&(bytes.len() as u64).to_be_bytes());
        self.hasher.update(bytes);
        if let EngineEvent::BarFrame(frame) = event {
            // Content evidence is independent of source row order and sequences.
            let payload = JsonValue::object([
                ("start", JsonValue::Int(frame.interval_start_ns)),
                ("end", JsonValue::Int(frame.interval_end_ns)),
                ("available_at", JsonValue::Int(frame.available_at_ns)),
                ("interval", JsonValue::String(frame.interval.clone())),
                (
                    "missing",
                    super::outcome::string_array(&frame.missing_symbols),
                ),
                (
                    "bars",
                    JsonValue::object(frame.bars_by_symbol.iter().map(|(symbol, bar)| {
                        (
                            symbol.clone(),
                            JsonValue::Array(
                                [bar.open, bar.high, bar.low, bar.close, bar.volume]
                                    .iter()
                                    .map(|value| JsonValue::String(value.to_string()))
                                    .collect(),
                            ),
                        )
                    })),
                ),
            ])
            .to_canonical_string();
            self.hasher.update(&(payload.len() as u64).to_be_bytes());
            self.hasher.update(payload.as_bytes());
        }
        self.dispatched_count += 1;
    }
}
