# Durable ingestion behavior

`unit/` contains private-adapter HTTP behavior tests, included by the owning module. No real Azure or provider service is contacted.

- `budget.rs` verifies the pure hard-limit rolling reservation state machine, including unused/lost permits, restart persistence and clock rollback.
- `budget_clock.rs` verifies reservations against the Blob authority's HTTP clock.
