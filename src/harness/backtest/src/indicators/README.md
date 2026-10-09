# Incremental native numerical indicators

This folder owns exact Decimal18 numerical kernels and the symbol-bound
`IndicatorMachine` that drives them. `registry.rs` resolves the supported
indicator kinds and parameters; momentum, overlays, volatility, and volume
modules contain their incremental arithmetic. These kernels do not load data,
submit orders, or implement the authored SDK ABI.

`TickIndicator::samples()` retains all completed, populated output windows,
including undefined warm-up values. Each sample carries symbol, interval,
actual causal availability, OHLC values, and driving observation count.
`in_progress()` exposes the current partial window separately. The machine
seals a bucket on a subsequent observed event or `advance(observed_time)`;
stopping never fabricates a future completion. Empty gaps are omitted and
values never carry forward into windows without driving observations.

Authored program indicators use the composable `IndicatorSequence` API in
`strategy-sdk`; native numerical kernels are a separate fixed-scale library.
Tests live in the crate's sibling `tests/` directory.
