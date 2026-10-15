# routes

HTTP handlers over catalog, coverage, query, and injected resolution/ensure ports. Option bars carry intervals through all routes. These handlers do not construct provider clients or deployment storage.

- `catalog.rs` — Authenticated catalog coverage route.
- `ensure.rs` — Authenticated ensure and job polling routes.
- `health.rs` — Probe-safe liveness and credential-check routes.
- `mod.rs` — Route modules and the shared unmatched-route fallbacks.
- `options.rs` — Resolve one individual contract before acquiring its ordinary datasets.
- `query.rs` — Authenticated file-query and bounded SQL-query routes.
- `raw.rs` — Authenticated raw-object metadata route.
- `reference.rs` — Authenticated point-in-time reference routes.

- `snapshot.rs` publishes checksum-pinned read manifests and renews signed locations without changing snapshot identity.
