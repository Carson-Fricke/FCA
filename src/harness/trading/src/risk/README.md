# Live risk

Deterministic risk decisions over injected execution context. This slice does not admit jobs or own market acquisition. `RiskPipeline` is the single rule-composition facade.

- `mod.rs` exports current risk contracts.
- `pipeline.rs` composes rule decisions and rejection policy.
- `models.rs` defines rule contexts, decisions, and audit data.
- `limits.rs` implements position, exposure, order, and loss limits.
- `kill_switch.rs` implements kill-switch state and actions.
- `collateral.rs` computes option collateral limits.
