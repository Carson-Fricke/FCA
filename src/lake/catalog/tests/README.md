# Catalog behavior verification

- `json_oracle.rs` verifies local catalog persistence against the canonical oracle.
- `run_record.rs` verifies run-record persistence and current-contract decoding.
- `oracle/` contains the canonical local catalog fixture.
- `unit/` contains SDK boundary tests included from the owning source module for private client construction.
