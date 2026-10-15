# Harness data-client behavior

- `batch.rs` verifies bounded acquisition batches, ordered results and explicit endpoint configuration.
- `options.rs` verifies external option acquisition waiting and acknowledgement identity.
- `streaming.rs` verifies execution-scoped manifest pinning, shared decoded buffers, fresh execution scopes and signed-object credential isolation.

The fake HTTP service replaces the external market-data API/object store. Production reading uses the real shared reader.
