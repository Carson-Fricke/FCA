# thesis_runtime/adapters/cosmos

Cosmos implementations for conversation records, sequence commits, payload chunks, execution history, work and ownership. It consumes the neutral codec and must not depend on filesystem or FCA domain stores.

- `__init__.py`: Cosmos DB persistence for the Thesis conversation timeline.
- `chunks.py`: Immutable, content-addressed JSON payload trees in an existing container.
- `client.py`: Cosmos DB composition for durable Thesis conversation state; local defaults use loopback HTTP to avoid the emulator's TLS buffering defect, while configured cloud endpoints retain TLS verification.
- `events.py`: Cosmos DB-backed durable execution-event store.
- `execution_store.py`: Workspace-partitioned workflow execution documents and phase artifacts.
- `notifications.py`: Subscription-driven API change-feed reader and local fan-out; independent from runnable-work cursors.
- `ownership.py`: Cosmos leases and transactional write fences in the workspace catalog. All fenced batch operations acquire document locks in sorted identity order, including admission's root update and progress's root fence, preventing root/counter deadlocks. Each SDK attempt receives fresh conditional options so fence retries retain data CAS.
- `records.py`: Workspace records, atomic message/cursor/chat/work admission, root ETag merge updates, and indexed history/input windows.
- `stack.py`: Compose all runtime adapters over one workspace-partitioned Cosmos catalog.
- `timeline.py`: Authoritative per-chat sequencing, transactional admission, identity reconciliation after uncertain responses, and post-commit timeline/work wake hints.
- `work.py`: Durable work, compact change-feed notifications, bounded metadata-only retained-work reconciliation, and local post-commit wake hints.
