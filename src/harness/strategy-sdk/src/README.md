# Author market consumers and export their ABI

This folder defines the authored Rust SDK. It depends on the decimal kernel
and serde, not the backtest engine or lake clients. Programs own their composed
indicators; there is no independent indicator plugin. Studies and indicators
have observation-only contexts, while strategies receive an order sink.
ABI 8 carries a callback-scoped `CExecutionSource`. The host serializes account,
owned orders and closed trades only on request, and the SDK memoizes each parsed
view independently. Actions cannot change the snapshot during the callback.
Current hosts and authored artifacts must rebuild together; older ABIs are rejected.

- `lib.rs` exposes the public authoring facade and SDK version.
- `consumer.rs` defines tagged subscriptions, the shared consumer trait,
  and individual-option input alias conventions for quote, trade, and bar data,
  canonical requirement unions, reusable `RoutingPlan` metadata and traversal
  storage, and guarded dependency-first routing. Direct routing callers create
  one plan after the consumer reaches its final address and pass it to each
  quote/trade/bar dispatch. Exported programs create their plan automatically.
  Cycles and zero-sized dependencies are rejected at plan construction.
- `program.rs` defines program construction, lifecycle, metrics, orders, and
  the separate screening surface. OrderSink exposes engine-owned execution state,
  typed order placement/cancellation and managed target_position/close_position.
  Final strategy state is available through MetricPublisher.execution(). Orders
  and closed trades have separate lazy orders()/closed_trades() accessors so
  account reads do not decode the full history on every tick.
- `execution.rs` defines read-only booked position/order/account and FIFO trade
  views, account-return/capital-utilization accessors and the market/limit/stop/
  stop-limit request constructors. It performs no fill accounting or matching.
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
  Optional low-level `FillLedger` accounts for deduplicated observed fills and
  per-order weighted price; `PendingOrder` observes cumulative fill quantity.
  These helpers do not own cancellation/rejection lifecycle.
  Other helpers include cash_return_bps, nearest-rank empirical_quantile,
  OLS log_price_slope_bps and a ratio-of-expected-energies Haar random-walk anchor.
  These helpers hold no market subscriptions or execution capabilities;
  authored consumers own symbol/cadence state and their output timing.
  Strategy position, pending-order and return calculations use the execution
  context instead of reconstructing them with this low-level observation helper.
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
  Boxed states freeze a routing plan after allocation. The router checks live
  declarations, dependency identity/order and cadence against that plan without
  rebuilding graphs per event. Market checks belong to routing; lifecycle
  callbacks retain their separate before/after checks.
  A validation failure permanently rejects subsequent callbacks on that state.

Tests live in the crate's sibling `tests/` folder. Engine replay, provider
access, and lake fetch planning do not belong in the SDK.

- `causal.rs`: Callback-local `DecisionContext` seals features through the injected host port; it offers no row update, clock override or outcome read.

Public capture names are unprefixed ASCII identifiers (1–64 letters/digits/dot/underscore/hyphen); the host owns `causal/` output names. Capture/metric/output host rejection fails the job even if ignored. SDK status categories are supplemented by exact host diagnostics in job completion. Numeric helpers reject unrepresentable outputs; temporal helpers reject elapsed-time overflow. See `docs/sdk-reference-and-diagnostics.md` in the repository for units and repair semantics.
