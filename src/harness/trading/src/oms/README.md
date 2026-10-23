# oms

This capability follows the ownership boundaries in `.github/architecture/repository.md`.

- `groups.rs`: Atomic lifecycle and exact accounting for multi-leg order groups.
- `manager.rs`: Single-leg order registry, replacement tracking, and expiry policy.
- `mod.rs`: Deterministic order-management service layer.
- `state.rs`: Execution-report-driven state and exact ledgers for one order.
