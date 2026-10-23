# runtime

- `go_live.rs`: The paper-to-live promotion gate.
- `mod.rs`: Paper and shadow runtime kernels.
- `paper.rs`: The paper-trading shell and its restorable supervision surface.
- `reconciler.rs`: State reconstruction and break detection for paper/live parity.
- `recovery.rs`: Startup recovery policy for the paper runtime.
- `shadow.rs`: Shadow-mode observe-only broker routing and comparison.
- `state_store.rs`: Durable runtime-state snapshots for the paper runtime.
