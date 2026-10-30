# src

This capability follows the ownership boundaries in `.github/architecture/repository.md`.

- `accounting.rs`: Immutable execution, fill, position, and account records.
- `error.rs`: Deterministic error surface for the live-trading port.
- `events.rs`: Immutable market-data event records.
- `fills/`: fills implementation and contracts.
- `ledger.rs`: Owns checked average-cost fill accounting shared by replay and live books.
- `lib.rs`: Owns deterministic trading policy, validated order types and accounting.
- `matching.rs`: Owns touch and stop-trigger policy for simulated execution in both runtimes.
- `market.rs`: Immutable scale-18 tick blocks, checked text ranges and callback-scoped
  market views. Owned live observations and compact historical ticks share one
  observation contract; preparation and persistence belong to the host.
- `oms/`: oms implementation and contracts.
- `orders.rs`: Immutable single- and multi-leg order records.
- `portfolio/`: portfolio implementation and contracts.
- `program.rs`: Owns program observation/order contracts, managed order actions
  and immutable strategy-scoped execution snapshots consumed by replay and authored adapters.
  Native consumers use `EngineMarket::view()` independently of event storage.
- `replay_errors.rs`: Backtest error taxonomy and abort, halt, teardown, and status policies.
- `replay_fills.rs`: Exact fill options shared with paper execution.
- `risk/`: risk implementation and contracts.
- `symbol.rs`: Immutable, canonical market-symbol value objects.

- `fee_policy.rs`: Owns checked raw fee formulas shared by replay and live execution.

- `notional_policy.rs`: Owns checked single-position notional caps shared by replay and live risk.

- `causal.rs`: Replay-owned immutable feature ledger and indexed exact-horizon outcomes; missing prices never carry forward. Depends on neutral contracts and exact numeric types, with no publication I/O.
