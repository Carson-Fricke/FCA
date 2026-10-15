# Compose the harness market-data capability

This hosting boundary selects explicit local or Azure adapters and injects the same catalog/acquisition capabilities into native execution and the HTTP boundary. It never constructs provider clients.

- `lib.rs`: exposes hosting composition.
- `api.rs`: builds the market-data router and native access port for one deployment.
- `runtime.rs`: wires Azure storage, catalog, durable admission and ACI reconciliation, and shares acquisition callbacks with local composition.
- `local.rs`: wires filesystem catalog/jobs, finite local workers and scoped HMAC object downloads.
- `access.rs`: adapts catalog, acquisition and object ports to native harness requests, preserving shared ensure policy and verified reading without HTTP self-calls.
