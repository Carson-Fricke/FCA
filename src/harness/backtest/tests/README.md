# Behavior verification

Tests exercise the public application and acquisition boundaries; fake transports replace external services. Pure numerical tests cover pricing arithmetic. Production implementation belongs to the sibling source folder.

- `backtest.rs` — Exact arithmetic of the deterministic synthetic market fixture.
- `canonical_trades.rs` — Canonical `metrics.json` trade and equity publication from the job ledger.
- `capital_fraction.rs` — A strategy instance's starting cash comes from its configured capital
- `configuration_identity.rs` — Behavior checks for configuration identity.
- `equity_metrics.rs` — Equity-curve metric rules at the public metrics boundary.
- `fee_arithmetic_failures.rs` — Fee arithmetic that leaves the exact decimal range fails by name.
- `indicator_sequences.rs` — Native indicator outputs preserve warm-up and final partial windows.
- `job_id_contract.rs` — Behavior checks for job id contract.
- `lake_parallel.rs` — Exact requested study acquisition and mandatory backtest
  quote/trade acquisition for bars-only, mixed and tick-only consumers.
- `null_transform.rs` — Signal-destruction null transform behavior and integration.
- `options_acknowledgement.rs` — Resolver acknowledgements must match the requested input before acquisition.
- `options.rs` — Research acquisition resolves each option once and retains contract attribution.
- `runtime_decimal_failures.rs` — Malformed, overflowed, and unknown values on the paper, shadow, reconciler,
- `study_frames.rs` — Atomic frame dispatch, separate backtest execution feeds
  and causal tick fills without expanding consumer callbacks.
- `streamed_replay.rs` — Streamed replay preserves dispatch and full valuation evidence; compressed histories retain every row, exact metrics and independent cloned snapshots.
