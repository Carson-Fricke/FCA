# Worker permit verification

- `budget.rs` verifies that a stalled reservation RPC cannot extend a permit's monotonic lifetime, slow/idle consumption reduces future batches without expiry-boundary oscillation, and early exhaustion grows only within the batch/configuration caps.

`budget.rs` also verifies that cached permits stay usable while an external authority refill is stalled.
