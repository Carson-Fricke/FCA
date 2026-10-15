# Finite ingestion process

Provider construction belongs to this process. Local and Azure deployments share the same transaction, progress, ownership, timeout and settlement logic; storage and credential adapters differ explicitly.

- `main.rs`: accepts one retained job ID and returns a finite process outcome.
- `identity.rs`: selects the ACI user-assigned identity through IMDS and caches resource-specific tokens.
- `runtime.rs`: composes local filesystem or Azure adapters, obtains worker credentials, executes shared transactions, records progress/results and settles the owned job. Local credentials can be read from an explicitly configured ignored provider environment file.
