# execution-storage/tests

Behavior fixtures and tests for the owning capability. No production implementation belongs here.

- `job_outcomes.rs`: Publication outcomes and terminal completion outbox durability, restart, acknowledgement conflicts and duplicate-admission behavior for the shared stores.

- `ownership.rs`: Storage-boundary guarantees: claim scope and admitted identity survive both adapters.

- `local_replacement.rs`: Open-reader-safe local replacement, no-clobber creation and actionable Windows sharing failures with staging-file cleanup.
