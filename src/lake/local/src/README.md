# Local harness market-data adapters

This capability supplies filesystem persistence and finite worker processes. It never calls providers or hosts HTTP routes; both deployments share ingestion application behavior and the harness reader.

- `lib.rs`: exports the local adapter facade.
- `state.rs`: resolves and initializes an explicitly owned filesystem tree and implements marker-protected cleanup.
- `catalog.rs`: stores option contract records; market coverage belongs to the shared manifest catalog.
- `ownership.rs`: protects cross-process job and publication ownership with kernel file locks.
- `jobs.rs`: durably admits immutable work and exposes fenced updates through the shared persistence ports.
- `runner.rs`: pins a worker executable by content hash, starts finite child processes, reconciles ownership after host restart and prohibits unknown-outcome replay.

`lib.rs` also snapshots available host RAM for retained local execution budgets; local fleet size follows that budget.

Finite local workers own scratch directories under `ingestion/scratch/<work-key>/`. Process cleanup removes that checked directory after the worker exits, including orphaned files from an interrupted process; system temporary storage is not left with unowned ingestion runs.
