# src  The lake facade exposes canonical OSI validation so acquisition routes individual options through asset_class=option while preserving the shared event interface.
- `bars.rs` — Exact OHLCV bar derivation from immutable trade events.
- `catalog.rs` — Provider-independent catalog records.
- `clock.rs` — Engine clock boundary.
- `error.rs` — Error types shared by the data layer.
- `events.rs` — Deterministic market-event value objects.
- `gap_healing.rs` — Deterministic gap detection against expected market-lake coverage.
- `history.rs` — Point-in-time historical market-data view with hard look-ahead clipping.
- `lake.rs` — Market-lake session and partition vocabulary.
- `lib.rs` — Historical and live market-data layer for the quant-harness Rust engine.
- `listing_events.rs` — Listing lifecycle events derived from the market-lake listings table.
- `options_universe.rs` — Point-in-time option contract universe with hard look-ahead clipping.
- `recorder.rs` — Live market-event recording in exact market-lake schemas.
- `replay.rs` — Deterministic, bounded replay of market-lake quote and trade events. Binds typed columns once per Arrow batch and exposes `map_arrow_events` for consuming conversion into caller-owned event representations.
- `security_master.rs` — Point-in-time security master over the market-lake listings table.
- `symbol.rs` — Canonical market-symbol validation shared with the market-lake contract.
- `time.rs` — UTC calendar primitives shared with the market-lake contract.
- `universe_view.rs` — Point-in-time strategy universe with hard look-ahead clipping.
- `ws.rs` — Transport-neutral Alpaca WebSocket feeds for the live shell.

Bars expose `OhlcvBar`, `BarAggregator`, and `build_bars`; redundant Python parity aliases and forwarding functions are removed.
