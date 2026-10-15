# Azure boundary behavior

- `azure_http.rs` drives the real admission, leasing and ACI adapters against a fake Azure HTTP service. It verifies joins/conflicts, ambiguous starts, shared worker caps, mismatched acknowledgements and stale-owner rejection.
