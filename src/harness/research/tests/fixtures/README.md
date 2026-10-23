# research/tests/fixtures

Behavior fixtures and tests for the owning capability. No production implementation belongs here.

- `mixed_strategy.rs`: Strategy fixture with a program-owned trade indicator and atomic bar orders.

- `mixed_study.rs`: Mixed study with a nested indicator dependency and observation-only context.

- `study_program.rs`: Authored study fixture: folds OHLCV bars and publishes exact bar reads.
