# Strategy execution SDK verification — 2026-10-27

## Scope and result

Implemented engine-owned accounting and execution capabilities for authored
backtest strategies. Strategies supply signals and desired execution policy;
they do not need an inventory/cash ledger, fill pairing, pending-exit sizing,
handwritten order identities or trade-return reconstruction.

- `OrderSink.execution()` reads the actual virtual book: signed inventory,
  average cost, cash, fees, realized/unrealized P&L, equity and exposure.
- `target_position` / `close_position` reconcile booked and pending quantities,
  including partial fills, direction changes and cancellation races.
- Typed market, limit, stop and stop-limit requests use platform-owned IDs.
  Explicit cancellation and DAY/GTC/IOC lifecycle are implemented by the venue.
- `orders()` / `order(id)` and `closed_trades()` expose owned lifecycle and FIFO
  financial evidence. The final histories/account are available to `on_stop`.
- Execution views and result publication use one checked FIFO matching kernel.
  Account returns use allocated initial capital; trade returns use matched entry
  notional. Average-cost current-book accounting and FIFO trade attribution keep
  their documented distinctions while inventory remains open.
- Immutable histories are shared across native snapshots, serialized when changed,
  and decoded by the SDK only on request. Reading a current position does not copy
  or parse the whole order/trade history on every quote.
- Canonical financial metrics and their undefined-reason fields cannot be replaced
  by custom strategy metrics. Results expose initial/final capital and full-stream
  peak gross exposure.

The authored replay fixture contains no fill callback or financial ledger. Its
10-share long entry at 100 and exit at 102, with 0.1 per-share fees on both sides,
reports gross 20, net 18 and 180 net basis points. SDK and report quantities agree.
The same fixture exercises a short, the four order constructors, and cancellation.

## Current contract and cutover

SDK ABI is 6. The loader rejects mismatched binaries; source/SDK/compiler hashes
invalidate cached artifacts and validation. Rebuild the harness and revalidate
authored programs before using this contract. No historical workspace or job data
was rewritten or deleted. No job-submission identity path was added or changed.

The basic template and SDK guidance use native execution state. The current
authoring guide's example no longer treats the first partial fill as a complete
entry or submits an assumed full-size exit. The new execution module and FIFO
kernel are included in engine reproducibility fingerprints.

## Verification

One `cargo test --workspace` run in `src/harness` passed. This preceded the final
history-sharing refinement and cancellation-retention regression. After those
changes, the affected boundary suites were rerun together and all 36 tests passed:

```powershell
cargo test -p backtest --test execution_api -p harness-research --test managed_execution --test mixed --test canonical_trades -p harness-strategy-sdk --test abi --test accounting -p harness-strategy-jit --test compile --test sdk_reference -p api --test job_metrics
```

These include seven managed-execution replay cases, thirteen canonical accounting
cases, real JIT source compilation, strategy/study capability isolation, ABI cache
rebuilds, SDK source references, and protection of undefined canonical metrics.

The selected Python run collected 202 tests across workspace programs, SDK identity
and the mandatory delivery architecture guard. It initially had 199 passes and
three template-related failures (111.83 seconds). The template's example comments
were made symbol-neutral and the two edit fixtures were updated for the current
unit struct. All three failed cases then passed together (5.00 seconds). The
architecture guard passed in the original run; no delivery implementation changed.

Passed final focused Ruff lint, Ruff format and ty checks for the changed Python
files and workspace test file. `uv lock --check`, the repository LF check,
`git diff --check`, and rustfmt checks of the changed Rust files passed. Tracked
hooks are configured as `.githooks`.

The final selected Rust package lint check passed:

```powershell
cargo clippy --no-deps -p harness-trading -p harness-strategy-sdk -p backtest -p harness-strategy-jit -p harness-research -p api -p mcp --all-targets -- -D warnings
```

Workspace-wide `cargo clippy --workspace --all-targets -- -D warnings` is blocked
by the existing `manual_is_multiple_of` finding in `num/src/decimal.rs:374`.
`cargo fmt --all --check` reports formatting outside this change, including
`backtest/event_bus.rs`, `backtest/history.rs`, and concurrent routing test changes.
Those unrelated files were preserved. The checkout contains other concurrent work;
this record describes the checks observed for this implementation, not a clean
repository-wide delivery of every pending change.

No real MSFT research run, production rollout, commit or push was performed.
