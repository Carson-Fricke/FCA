# Azure boundary behavior

- `azure_http.rs` drives the real admission, leasing and ACA Job execution adapters against a fake Azure HTTP service. It verifies duplicate admission, interrupted POST reconciliation without replay, shared worker caps, bounded disjoint partition fleets, one public job aggregation, acknowledgement and image attribution, stale-owner fencing, heartbeat/lost-lease cancellation, and concurrent Blob ETag request-budget reservations.
  Start fixtures follow the vendor contract: minimal `200` name/id responses or empty `202` operation responses, followed by a full execution GET. Coverage includes pending operations, unsafe locations, name/resource-ID/template mismatches and one retained POST through reconciliation.
