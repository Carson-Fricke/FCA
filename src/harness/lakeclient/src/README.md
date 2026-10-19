# Harness market-data access

Callers own waiting and execution policy. A combined host injects native catalog/acquisition ports; separate clients use authenticated control HTTP and scoped object downloads. Both read through the same verified reader and cache.

- `lib.rs`: publishes the intentional client facade.
- `access.rs`: defines the injected application capability port and typed requests.
- `blocking.rs`: bridges synchronous engine callers to async access.
- `client.rs`: implements acquisition waiting, progress and shared reading over native or remote access.
- `ensure.rs`: implements pure coverage verdicts and polling cadence.
- `error.rs`: declares typed failures.
- `models.rs`: declares current response views.
- `snapshot.rs`: pins execution-scoped manifests and retrieves signed compressed objects without forwarding API credentials.
- `spec.rs`: validates requested windows and intervals.
- `testing.rs`: supplies fake external HTTP boundaries for behavior tests.

The standalone client defaults to the local combined harness at `http://127.0.0.1:8001/market-data`. Remote executions explicitly configure `LAKE_API_URL` and `LAKE_API__BEARER_TOKEN`. The native host injects access directly.

Ensure completion polling starts at 100 ms and caps at 500 ms, avoiding multi-second completion detection delays.

`client.rs` and `blocking.rs` expose replay cursors sharing pinned manifests and
content caches with independent reader admission. Replay reopen does not emit
acquisition progress; the engine reports its actual read and replay row counts.
