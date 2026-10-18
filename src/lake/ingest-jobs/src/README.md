# Durable ingestion authority and dispatch

One current job document retains immutable requests and the accepted worker specification alongside owned lifecycle state. Application admission and reconciliation depend on execution and persistence ports. They never fetch provider data or decode partitions.

- `lib.rs`: exports the service, current contracts and adapter facades.
- `model.rs`: validates dataset/option work and durable lifecycle fields.
- `execution.rs`: declares accepted local/ACA partition execution specifications and adapts ACA execution to the worker port.
- `ports.rs`: defines worker, persistence and ownership contracts and the shared heartbeat guard.
- `store.rs`: implements conditional Azure Blob admission, leases and the read-only job repository.
- `aca.rs`: validates Azure configuration and starts/reconciles exactly attributed ACA Job executions. Start responses contain only an execution name/resource ID; the adapter validates that identity then GETs the exact execution to verify its retained job/partition/image. Accepted asynchronous starts use bounded GET-only polling of same-origin, same-subscription operation locations. Unresolved outcomes are reconciled by attribution without replaying POST.
- `budget.rs`: defines restart-safe rolling request reservations and the conditional Blob adapter.
- `service.rs`: reconciles retained intents under dispatcher ownership and enforces shared worker limits and deadlines.
- `bin/`: contains the finite provider-free Azure coordination entrypoint.

Request allowance uses its own `RequestAuthority` port; permit caches do not depend on execution lifecycle methods. Immutable worker lanes contain several disjoint bounded chunks, with concurrently persisted dispatch intents and durable final metrics.
