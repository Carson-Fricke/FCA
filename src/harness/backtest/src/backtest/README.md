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

`job.rs` computes the canonical ledger from those checkpoints and engine fills.
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
