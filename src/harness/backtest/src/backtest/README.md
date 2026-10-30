# Deterministic backtest execution

This folder owns deterministic replay coordination, validated replay settings,
preflight and causal valuation. Shared trading policy lives in `trading`;
acquisition, job composition and artifact publication live in `research`.
`config.rs` normalizes accepted execution configuration;
`fixture.rs` supplies synthetic replay events; `preflight.rs` verifies
coverage; `engine_core.rs` drives native strategy runtimes. `null.rs` transforms
supported bar-close return nulls before provenance fingerprints are derived.

The native engine updates `valuation.rs` after market events, completed bar
frames, and booked fills. It accounts for each fill once, using only observed
marks; bar marks become available at frame dispatch. Unchanged financial states
retain their exact logical sample count and final observed timestamp. Unused
configured capital remains cash; negative or excessive allocations are rejected.

The sibling `research` capability consumes ensured lake Arrow batches incrementally into replay events.
Historical ticks retain immutable bounded blocks through native dispatch and the
authored ABI, without allocating per-event symbols, venues or identifiers on
prepared-cache hits. Exact scale-18 coefficients become variable-scale views
without re-rounding. Owned synthetic/live observations use the same market view.
Null transforms materialize original source values before their existing checked
rescaling; they never rescale already-rounded observations.
`research/src/market_replay.rs` merges chronological per-symbol/type cursors with bounded lookahead,
plans coverage and provenance from committed catalog snapshots without scanning ticks.
Only bars needed for consumer frame assembly are read before simulation; the
reader verifies immutable objects during the one tick replay. Each pull cursor has an independent decode
slot to avoid head-of-line deadlock while sharing immutable caches.
`history.rs` retains compact financial states in 8,192-row compressed,
checksummed scratch frames, with independent clone snapshots and a configurable
2 GiB per-history scratch cap (`HARNESS_HISTORY_SCRATCH_BYTES`). Financial metrics
consume the full logical sample stream, including repeated flat samples, with
the original decimal mean and ordered floating-point deviation sum. Reporting
keeps observed first/minimum/maximum/last values per minute bucket and every fill
boundary. It collapses constant spans rather than inventing a regular grid;
Parquet and HTML use this reporting history. Metrics declare logical and retained
counts and reporting cadence separately. Replay bounds
are 32 symbol/type cursors and 65,536 events sharing one timestamp per cursor;
exceeding a bound fails explicitly.
Studies acquire the compiled program-and-indicator subscription union; backtests
also acquire quotes and trades for every input symbol for `simple_touch`
execution. Execution feeds do not expand consumer callbacks or the subscription
echo. Bars use one canonical minute source and never fill orders themselves.

The sibling `research` capability reports native execution, result computation and artifact-writing stages through the injected lake
client progress port, and summarizes the causal ledger without replaying fill accounting.
It preserves all configured initial capital, values open positions between
fills and after the final fill. Full-stream metrics remain independent of chart
sampling. Its FIFO closed-trade table is the sole authority
for realized trade slices and allocated entry/exit fees. Open fees remain in
unrealized PnL. Marked headline net PnL equals final equity minus initial capital;
gross PnL adds all execution fees. Closed-trade net PnL remains separately named.
No caller reconstructs another trade ledger for these results.

Tests live in the crate's sibling `tests/` folder. Authored ABI loading belongs
to `strategy-jit`; job records, admission and fenced ownership belong to `execution`;
HTTP/MCP adapt those application capabilities.

Option input aliases resolve once at as_of no later than replay start. Acquisition fetches quotes/trades/bars by the resolved OSI symbol, remaps callbacks to the alias, and retains selector and contract evidence in fixture source provenance. Option-specific execution accounting is outside this market-data change.

`manifest.rs` always serializes effective, acquired, and execution subscription
plans, including empty plans. Acquired inputs are the normalized consumer source
plan plus backtest execution feeds; execution inputs are quote/trade feeds for
every backtest input symbol, empty for studies. No legacy omission is supported.

Catalog fingerprints hash the pinned manifest inventory and exact accepted window;
`replay_row_fingerprint` is explicitly null when no materialized row digest exists.
Synthetic materialized fixtures retain their canonical row digests. This describes
source evidence, not selectable historical contract versions. In-window replay
counts are finalized from observed rows; full-day plans also verify catalog totals.
Missing objects, checksums, schemas and physical row counts fail in the reader.
Null transformations derive provenance from the pinned inputs and deterministic
transform, and transform only bars during planning; ticks transform during replay.

## Files

- `mod.rs` declares the backtest modules and public facade.
- `config.rs` validates and normalizes execution inputs.
- `engine_core.rs` owns causal dispatch and strategy execution.
- `execution.rs` owns scoped order actions, risk admission, managed signed targets
  and booked execution snapshots. It cancels conflicting working exposure, waits
  for in-flight fills and stops automatic retries after a risk rejection. Only
  engine-created managed orders can satisfy a target's projected inventory;
  protective stops and other explicit orders must be cancelled before flattening.
- `fees.rs` assesses and aggregates execution fees.
- `fixture.rs` loads and fingerprints fixture data.
- `history.rs` stores bounded, compressed scratch histories with reusable codec
  workspaces and snapshot-isolated scratch ownership.
- `manifest.rs` records accepted inputs and acquired data provenance.
- `null.rs` applies declared null transformations.
- `portfolio.rs` owns portfolio and allocation accounting.
- Strategy views read the owning portfolio book; they do not reconstruct a book
  inside authored code. Venue order views distinguish scheduled from booked fills
  and retain rejected/unfilled/canceled/expired orders. Day expiration uses the
  acquired exchange session boundaries; IOC cancels its unfilled remainder.
- `preflight.rs` validates coverage before replay.
- `rng.rs` supplies deterministic seeded randomness.
- `screen.rs` executes screening programs.
- `stream.rs` defines re-iterable replay inputs and their evidence contract.
- `valuation.rs` owns incremental valuation, exact metric weighting and sparse
  reporting. Its bounded return cache avoids recalculating exact returns and
  rereading financial states during the variance pass.
- `yaml.rs` reads the supported configuration syntax.

- `event_bus.rs`: Owns deterministic dispatch ordering and its hash. A dedicated
  feed head bypasses heap operations in the common case; generated events and
  ties retain the full ordering key. Reuses identity storage and timestamp
  prefixes without changing canonical bytes; no venue or strategy policy.

- `identity.rs`: Builds replay provenance and configured program identities; no execution.

- `outcome.rs`: Owns immutable replay outcomes and canonical artifact projection; no execution or publication.

- `projection.rs`: Precomputes run-scoped numeric scales and consumes replay events into program observations without copying identifiers; no acquisition.

- `risk.rs`: Projects validated replay rules into shared trading risk policy; no execution or persistence.

- `subscriptions.rs`: Owns replay requirement normalization and causal bar-frame assembly; no execution or storage.

- `venue.rs`: Owns resting-order lifecycle, stable per-symbol active/pending-fill
  indexes and deadline-scheduled day expiration. Completed orders remain in
  history but are not scanned for matching, expiration or managed targets on
  market events. Due expiration waits for committed fills to book before it
  expires the remaining quantity at the next market boundary.
