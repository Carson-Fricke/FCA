# research/tests/fixtures

Behavior fixtures and tests for the owning capability. No production implementation belongs here.

- `mixed_strategy.rs`: Strategy fixture with a program-owned trade indicator and atomic bar orders.

- `mixed_study.rs`: Mixed study with a nested indicator dependency and observation-only context.

- `study_program.rs`: Authored study fixture: folds OHLCV bars and publishes exact bar reads.

- `managed_strategy.rs`: Strategy policy through execution context targets and typed
  order constructors, with no inventory, pending-fill or cash ledger.

- `causal_program.rs`: Compiled capture fixture with adversarial modes; studies and strategies share the same observation requests.
