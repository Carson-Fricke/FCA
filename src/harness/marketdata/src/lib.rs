//! Historical and live market-data layer for the quant-harness Rust engine.
//!
//! This crate is the staged Rust port of `src/harness/data`. It follows the
//! Python module boundaries so behavior and on-disk contracts can be checked
//! one module at a time:
//!
//! - [`lake`] — market-lake sessions and the Hive partition vocabulary.
//! - [`bars`] — exact OHLCV derivation from immutable trade events.
//! - [`history`] — bounded point-in-time history with look-ahead clipping.
//! - [`security_master`] / [`listing_events`] — point-in-time listings.
//! - [`options_universe`] / [`universe_view`] — point-in-time universes.
//! - [`replay`] — deterministic bounded replay of decoded route events.
//! - [`gap_healing`] — live-feed gap detection against expected coverage.
//! - [`ws`] / [`recorder`] — live ingest and recording.
//!
//! No code path in this crate opens a lake partition path: market-lake reads
//! stream through `harness-lakeclient`, and the decode boundary here turns the
//! route's Arrow IPC batches into normalized events. No code path reads wall
//! time, consumes randomness, touches the network, or performs I/O. Every
//! instant comes from an injected clock.

pub mod bars;
pub mod catalog;
pub mod clock;
pub mod error;
pub mod events;
pub mod gap_healing;
pub mod history;
pub mod lake;
pub mod listing_events;
pub mod options_universe;
pub mod recorder;
pub mod replay;
pub mod security_master;
pub mod symbol;
pub mod time;
pub mod universe_view;
pub mod ws;

pub use bars::{build_bars, interval_delta, BarAggregator, OhlcvBar};
pub use catalog::{
    ContractRecord, ContractsCatalogPort, DailyFrontSet, DelistingType, ListingRecord, ListingType,
    ListingsCatalogPort, MemoryContractsCatalog, MemoryListingsCatalog, OptionType,
};
pub use clock::{Clock, ManualClock};
pub use error::DataError;
pub use events::{
    cutoff_key, end_key, ordering_key, start_key, BarEvent, EventType, MarketEvent, OrderingKey,
    QuoteEvent, TradeEvent,
};
pub use gap_healing::{CoverageCalendar, CoverageGap, LakeSessionCalendar, LiveGapDetector};
pub use history::{
    HistoricalView, HistoryAggregate, HistorySelector, HistorySubscription, StrategyHistory,
    DEFAULT_HISTORY_CAPACITY,
};
pub use lake::{
    crypto_sessions, crypto_utc_days, partition_path, sessions_for, symbol_filename, xnys_sessions,
    AssetClass, BarInterval, DataType, ExchangeSession,
};
pub use listing_events::{
    CorporateActionEvent, CorporateActionKind, ListingEventDeriver, PessimisticDelistingSettlement,
};
pub use options_universe::{
    DteFilter, MoneynessFilter, OptionUniverseSnapshot, OptionUniverseSpec, OptionUniverseView,
    SelectionMode, StrikeFilter, UnderlyingView,
};
pub use recorder::{
    event_row, live_part_path, schema_columns, LiveObjectStore, LiveRecorder, LiveRecorderConfig,
    LocalLiveObjectStore, RecordedRow, SessionResolver, UtcDaySessionResolver, XnysSessionResolver,
};
pub use replay::{
    events_from_arrow, events_from_parquet, map_arrow_events, LakeReplayFeed, ReplayFingerprints,
    ReplayStream,
};
pub use security_master::{SecurityMasterView, SecuritySnapshot};
pub use symbol::{canonical_osi_symbol, canonical_symbol, crypto_pair, is_osi_symbol};
pub use time::{Date, DateTime, Nanos};
pub use universe_view::{
    EquityListingUniverse, MemberKind, OptionContractUniverse, UniverseComposition, UniverseMember,
    UniverseSnapshot, UniverseSource, UniverseView,
};
pub use ws::{
    backoff_delay, opra_rotation_payloads, rotation_delta, AlpacaCryptoInbox,
    AlpacaCryptoSubscription, AlpacaCryptoWebSocketFeed, AlpacaInbox, AlpacaLiveConfig,
    AlpacaLiveFeed, AlpacaMessage, AlpacaOpraInbox, AlpacaOpraLiveFeed, AlpacaOpraSubscription,
    AlpacaOpraWebSocketFeed, AlpacaSipInbox, AlpacaSipSubscription, AlpacaSipWebSocketFeed,
    AlpacaWebSocketFeed, AlpacaWebSocketProtocolError, AsyncClock, AsyncWebSocketConnection,
    AsyncWebSocketTransport, InboxKind, OpraRotationDelta, StaleHandler, StopSignal,
    TungsteniteTransport,
};

/// The persisted market-data schema version shared with `lake.contracts`.
pub const SCHEMA_VERSION: &str = "1";
