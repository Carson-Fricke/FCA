# Ingestion pipeline resources

This capability owns shared resource admission, cancellation and measurements for providers, ingestion, storage and catalog publication. It does not fetch, encode, persist data or own job lifecycle policy.

- `Cargo.toml` declares the capability and OS measurement dependencies.
- `src/` implements the admission/measurement contract.
- `tests/` verifies saturation, independent budgets and cancellation fencing.
