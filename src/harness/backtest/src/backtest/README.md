# Deterministic backtest execution

This folder owns replay configuration, fixture acquisition, preflight, native
market/frame/fill dispatch, simulated execution, portfolio accounting, and job
artifact composition. `config.rs` normalizes accepted execution configuration;
`fixture.rs` and lake adapters supply replay events; `preflight.rs` verifies
coverage; `engine_core.rs` drives native strategy runtimes. `null.rs` transforms
supported bar-close return nulls before provenance fingerprints are derived.

The native engine records causal valuation checkpoints after market events,
completed bar frames, and booked fills. Each checkpoint identifies the exact
fill prefix and sparse observed mark updates; bar marks become available at
frame dispatch time. The engine retains unused configured capital in a residual
cash book and rejects negative or excessive allocations.

`job.rs` consumes ensured lake Arrow batches incrementally into replay events.
`stream.rs` merges chronological per-symbol/type cursors with bounded lookahead,
collects exact preflight fingerprints without retaining ticks and reopens the
same pinned snapshot for simulation. Each pull cursor has an independent decode
slot to avoid head-of-line deadlock while sharing immutable caches.
`history.rs` retains exact valuation and ledger rows in 8,192-row compressed,
checksummed scratch frames, with independent clone snapshots and a configurable
2 GiB per-history scratch cap (`HARNESS_HISTORY_SCRATCH_BYTES`). Financial metrics
and Parquet consume every row; only HTML chart points are sampled. Replay bounds
are 32 symbol/type cursors and 65,536 events sharing one timestamp per cursor;
exceeding a bound fails explicitly.
Studies acquire the compiled program-and-indicator subscription union; backtests
also acquire quotes and trades for every input symbol for `simple_touch`
execution. Execution feeds do not expand consumer callbacks or the subscription
echo. Bars use one canonical minute source and never fill orders themselves.

`job.rs` reports native execution and artifact-writing stages through the injected lake
client progress port, and computes the canonical ledger from those checkpoints and engine fills.
It preserves all configured initial capital, values open positions between
fills and after the final fill, and writes the same equity history into metrics,
reports, and Parquet artifacts. Its FIFO closed-trade table is the sole authority
for realized trade slices and allocated entry/exit fees. Open fees remain in
unrealized PnL. Marked headline net PnL equals final equity minus initial capital;
gross PnL adds all execution fees. Closed-trade net PnL remains separately named.
No caller reconstructs another trade ledger for these results.

Tests live in the crate's sibling `tests/` folder. Authored ABI loading belongs
to `strategy-jit`; request admission and external result projection belong to
the harness API and MCP packages.

Option input aliases resolve once at as_of no later than replay start. Acquisition fetches quotes/trades/bars by the resolved OSI symbol, remaps callbacks to the alias, and retains selector and contract evidence in fixture source provenance. Option-specific execution accounting is outside this market-data change.

`manifest.rs` always serializes effective, acquired, and execution subscription
plans, including empty plans. Acquired inputs are the normalized consumer source
plan plus backtest execution feeds; execution inputs are quote/trade feeds for
every backtest input symbol, empty for studies. No legacy omission is supported.
