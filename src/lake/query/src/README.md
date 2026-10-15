# Query application

Provider-independent coverage and snapshot planning and bounded read-only SQL. Ports are injected; this slice never dispatches provider work itself or owns HTTP transport.

- `lib.rs`: intentional query application exports.
- `application.rs`: file-query validation and availability orchestration.
- `bounded.rs`: bounded DuckDB execution and read-only SQL guards.
- `error.rs`: query rejection contract.
- `models.rs`: requests, results and injected query ports.
- `read.rs`: canonical snapshot planning shared by native and HTTP callers, including option membership.
- `reference.rs`: point-in-time reference projections.
- `sql.rs`: SQL application orchestration.
