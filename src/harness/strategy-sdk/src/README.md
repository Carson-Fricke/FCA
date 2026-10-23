# Author market consumers and export their ABI

This folder defines the authored Rust SDK. It depends on the decimal kernel
and serde, not the backtest engine or lake clients. Programs own their composed
indicators; there is no independent indicator plugin. Studies and indicators
have observation-only contexts, while strategies receive an order sink.

- `lib.rs` exposes the public authoring facade and SDK version.
- `consumer.rs` defines tagged subscriptions, the shared consumer trait,
  and individual-option input alias conventions for quote, trade, and bar data,
  canonical requirement unions, per-consumer routing signatures, and guarded
  dependency-first routing. Cycles and zero-sized dependencies are rejected.
- `program.rs` defines program construction, lifecycle, metrics, orders, and
  the separate screening surface.
- `outputs.rs` defines causal series, confirmed events, scalar-cell tables,
  independently published dataset semantics and saved research views,
  and frozen fitted state with training-job/window provenance. The host validates
  and persists these through `MetricPublisher.publish_output`. Composed indicator
  OHLC histories are automatically published by dependency path at stop, with
  completed and in-progress tables kept separate and decimals preserved as strings.
- `building_blocks.rs` provides trailing moments, EMA, simple-average RSI/ATR,
  rolling VWAP, crossover age, directional-change confirmation, trailing-window
  vertical-distance PIP and frozen normalization. The Haar kernel is internal to
    evidence publication; trading wavelet features use the managed indicator.
    Its `FillLedger` accounts for every deduplicated actual fill and per-order
    weighted price. `PendingOrder` tracks requested versus cumulatively filled
    quantity; a partial fill does not complete an order. Cancellation/rejection
    requires explicit lifecycle evidence from the caller.
  It also provides FillLedger for exact partial-fill inventory/cash/fee accounting,
  cash_return_bps with entry quantity and price, nearest-rank empirical_quantile,
  OLS log_price_slope_bps and a ratio-of-expected-energies Haar random-walk anchor.
  These helpers hold no market subscriptions or execution capabilities;
  authored consumers own symbol/cadence state and their output timing.
- `wavelet.rs` implements managed completed-bar Haar features with private per-symbol
  state, exact-decimal scale energy, signed trend, explicit missing/warm-up and
  immutable availability-bounded snapshots. Programs own WaveletIndicator through
  MarketConsumer dependencies and retain snapshots at original setup decisions.
  This path prevents future observations from changing historical features; it does
  not restrict arbitrary Rust computations or prove fabricated studies truthful.
- `research/` defines validated saved-view and dataset-semantic descriptors, evidence publishers and
  reusable family compositions without implementing new analysis algorithms.
- `indicator.rs` defines symbol-aware values, mandatory `IndicatorSequence`
  history, and decimal arithmetic. Every owned indicator exposes `value(symbol)`,
  `sequence()`, and `sequence_mut()`; the shared router records its outputs after
  dependencies and callbacks and before the owner sees the event. Bar-derived
  output preserves each source frame's interval, availability, and missing
  symbols. Undefined/warm-up values remain explicit, including partial windows.
  Tick indicators construct `IndicatorSequence::ticks(interval_ns)` to request
  output fidelity. Accepted trade/quote values form per-symbol OHLC output bars
  on epoch-aligned half-open windows. Empty gaps are omitted; values never carry
  forward across missing input. `completed()` excludes `in_progress()`. Only
  observed causal time advances seal windows; stopping does not invent a final
  completed window. Mutable output cadence is rejected by frozen routing checks.
- `events.rs` defines borrowed exact-decimal quotes, trades, interval-identified
  atomic frames, fills, and startup facts.
- `abi.rs` defines versioned C layouts and program vtables. Requirements cross
  as a state-owned JSON array after construction.
- `export.rs` defines panic-guarded export macros and trampolines. All market
  calls route through each consumer's own subscriptions after dependencies.
  Boxed states freeze routing signatures after allocation; lifecycle and market
  callbacks validate declarations and topology before and after execution.
  A validation failure permanently rejects subsequent callbacks on that state.

Tests live in the crate's sibling `tests/` folder. Engine replay, provider
access, and lake fetch planning do not belong in the SDK.
