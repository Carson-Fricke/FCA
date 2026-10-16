# Compose the harness market-data capability

This hosting boundary selects explicit local or Azure adapters and injects the same catalog/acquisition capabilities into native execution and the HTTP boundary. It never constructs provider clients.

- `lib.rs`: exposes hosting composition.
- `api.rs`: builds the market-data router and native access port for one deployment.
- `runtime.rs`: wires Azure storage, catalog, durable admission and ACA reconciliation, and shares acquisition callbacks with local composition.
- `local.rs`: wires filesystem catalog/jobs, finite local workers and scoped HMAC object downloads.
- `access.rs`: adapts catalog, acquisition and object ports to native harness requests, preserving shared ensure policy and verified reading without HTTP self-calls.

`LAKE_LOCAL_PUBLIC_URL` selects the external market-data base for signed local object downloads when a proxy, LAN name or forwarded port differs from the bind address.
