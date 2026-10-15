# Durable ingestion authority and dispatch

One current job document retains immutable requests and the accepted worker specification alongside owned lifecycle state. Application admission and reconciliation depend on execution and persistence ports. They never fetch provider data or decode partitions.

- `lib.rs`: exports the service, current contracts and adapter facades.
- `model.rs`: validates dataset/option work and durable lifecycle fields.
- `execution.rs`: declares accepted local/ACI execution specifications and adapts ACI execution to the worker port.
- `ports.rs`: defines worker, persistence and ownership contracts and the shared heartbeat guard.
- `store.rs`: implements conditional Azure Blob admission, leases and the read-only job repository.
- `aci.rs`: validates Azure configuration and controls deterministic container groups.
- `service.rs`: reconciles retained intents under dispatcher ownership and enforces shared worker limits and deadlines.
- `bin/`: contains the finite provider-free Azure coordination entrypoint.
