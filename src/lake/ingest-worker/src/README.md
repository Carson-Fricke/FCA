# Finite ingestion process

Provider construction belongs to this process. Local and Azure deployments share the same transaction, progress, ownership, timeout and settlement logic; storage and credential adapters differ explicitly.

- `main.rs`: accepts the retained job ID and internal partition key, then returns a finite process outcome with structured failure/resource evidence.
- `budget.rs`: adapts local/Blob fleet reservations to the provider request-admission port. One-second batches start at 32 permits, shrink to observed consumption after expiry, and grow only after exhaustion within the first half of their lifetime. Unused permits remain charged; delayed RPCs never extend monotonic expiry.
- `identity.rs`: selects the ACA user-assigned identity through the ACA identity endpoint and caches resource-specific tokens.
- `runtime.rs`: composes local filesystem or Azure adapters, obtains worker credentials, executes shared transactions, initializes shared resource limits, records stage/resource metrics and progress/results, fences surviving blocking publishers on cancellation, and settles the owned job. Local credentials can be read from an explicitly configured ignored provider environment file.

A finite process keeps its original anchor owned while it claims additional
unstarted compatible chunks. Provider clients, credentials and budget permits
are reused; per-chunk counters are deltas and lifetime resource peaks are labeled.
Only a failure cancels the process pipeline. Neither a stopped worker nor a
replacement process can replay a chunk already marked running.
