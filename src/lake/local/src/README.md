# Local harness market-data adapters

This capability supplies filesystem persistence and finite worker processes. It never calls providers or hosts HTTP routes; both deployments share ingestion application behavior and the harness reader.

- `lib.rs`: exports the local adapter facade.
- `state.rs`: resolves and initializes an explicitly owned filesystem tree and implements marker-protected cleanup.
- `catalog.rs`: indexes current coverage and contract records in independently atomic files, avoiding whole-catalog rewrites.
- `ownership.rs`: protects cross-process job and publication ownership with kernel file locks.
- `jobs.rs`: durably admits immutable work and exposes fenced updates through the shared persistence ports.
- `runner.rs`: pins a worker executable by content hash, starts finite child processes, reconciles ownership after host restart and prohibits unknown-outcome replay.
