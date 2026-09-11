//! Historical and live market-data layer for the quant-harness Rust engine.
//!
//! This crate is the staged Rust port of `src/harness/data`. It follows the
//! Python module boundaries so behavior and on-disk contracts can be checked
//! one module at a time:
//!
//! - [`lake`] — synchronous market-lake access, sessions, and Hive paths.
//! - [`bars`] — exact OHLCV derivation from immutable trade events.
//! - [`history`] — bounded point-in-time history with look-ahead clipping.
//! - [`security_master`] / [`listing_events`] — point-in-time listings.
//! - [`options_universe`] / [`universe_view`] — point-in-time universes.
//! - [`replay`] / [`options_replay`] — deterministic bounded replay.
//! - [`gap_healing`] — live-feed gap detection and lake backfill.
//! - [`ws`] / [`recorder`] — live ingest and recording.
//!
//! No code path in this crate reads wall time, consumes randomness, touches
//! the network, or performs I/O. Every instant comes from an injected clock.

pub mod bars;
pub mod catalog;
pub mod clock;
pub mod error;
pub mod events;
pub mod gap_healing;
pub mod history;
pub mod lake;
pub mod lake_reader;
pub mod listing_events;
pub mod options_replay;
pub mod options_universe;
pub mod recorder;
pub mod replay;
pub mod security_master;
pub mod symbol;
pub mod time;
pub mod universe_view;
pub mod ws;

pub use bars::{build_bars, interval_delta, Bar, BarAggregator, BarBuilder, Ohlcv, OhlcvBar};
pub use catalog::{
    ContractRecord, ContractsCatalogPort, DailyFrontSet, DelistingType, ListingRecord, ListingType,
    ListingsCatalogPort, MemoryContractsCatalog, MemoryListingsCatalog, OptionType,
};
pub use clock::{Clock, ManualClock};
pub use error::DataError;
pub use events::{
    cutoff_key, end_key, ordering_key, start_key, EventType, MarketEvent, OrderingKey, QuoteEvent,
    TradeEvent,
};
pub use gap_healing::{
    CoverageCalendar, CoverageGap, HealingAction, HealingStatus, LakeGapBackfiller,
    LakeSessionCalendar, LiveGapDetector, LiveGapHealer, UncoveredGapError,
};
pub use history::{
    HistoricalView, HistoryAggregate, HistorySelector, HistorySubscription, StrategyHistory,
    DEFAULT_HISTORY_CAPACITY,
};
pub use lake::{
    crypto_utc_days, partition_path, resolve_sessions, symbol_filename, xnys_sessions, AssetClass,
    CoverageEntry, CoverageResponse, CoverageState, DataType, DatasetRequest, EnsureAccepted,
    EnsureResponse, ExchangeSession, FileQueryFile, FileQueryResponse, FileQueryState,
    FileQuerySummary, JobStatus, LakeClientPort, MarketLakeAdapter, MarketLakeLoad,
    MarketLakeRequest,
};
pub use lake_reader::{write_lake_partition, LocalLakeReader};
pub use listing_events::{
    CorporateActionEvent, CorporateActionKind, ListingEventDeriver, PessimisticDelistingSettlement,
};
pub use options_replay::{
    MemoryOptionPartitionPort, OptionPartitionPort, OptionsDayPlan, OptionsReplayFeed,
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
    events_from_parquet, LakeObjectSource, LakeReplayFeed, LocalObjectSource, MemoryObjectSource,
    ParquetFileReader, ParquetReader, ReplayFingerprints, ReplayStream, StaticReader,
};
pub use security_master::{SecurityMasterView, SecuritySnapshot};
pub use symbol::{canonical_osi_symbol, canonical_symbol, is_osi_symbol};
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
