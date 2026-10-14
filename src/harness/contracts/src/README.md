# Shared harness contracts

This folder defines execution-neutral wire and value contracts. It depends on serialization and numeric primitives, never on jobs, workflows, providers, or UI.

- `lib.rs`: Exports the shared contracts and defines intervals, authoring metadata, market requirements and result values.
- `progress.rs`: Defines execution stages, observed dataset counters and the caller-injected progress callback; persistence remains outside contracts.

Individual-option selectors and resolution evidence are re-exported from lake-contracts, a provider-independent value dependency. Jobs, workflows, provider clients, and ingestion implementations remain outside this contract crate.
