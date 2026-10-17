# Release verification

- `installed_sdk_smoke.py` verifies the complete portable Cargo path-dependency
  graph and exercises public program templates, SDK identity, and
  reference reads from an installed wheel with Python isolated from the checkout.
- `wait_aca_revision.sh` waits for the actual latest Azure Container App revision,
  validates its image digest and ready containers, and requires API probe health.
  It never accepts a previously healthy revision as evidence of a new rollout.

Build and deployment checks belong here, outside the pytest behavior suite.

- `merge_cargo_recipes.py` composes the pinned cargo-chef recipes for both Rust
  workspaces in either direction. All local crates become dependency-build stubs
  together; the selected workspace's lockfile stays authoritative, and real
  sources are restored afterward. The lake dependency layer feeds independent
  harness and finite-worker build branches without mutable target-cache sharing.
  Production recipes omit test/example/bench target stubs while retaining the
  locked dependency metadata, so adding a test does not invalidate binary caches.
- `prepare_python_sources.py` derives Python source and explicit package-data
  inputs from `pyproject.toml`. Both image builders install the complete wheel
  from this filtered tree, so unrelated Rust/UI files cannot invalidate Python
  installation layers. Installed SDK smoke remains required for both images.
- `changes.mjs` derives dependency-aware workflow selection from the commit range;
  `node --test scripts/ci/changes.test.mjs` verifies source/build input routing.
- `image-retention.mjs` owns the pure bounded image-retention policy. Retained
  roots protect all platform manifests, attestations and nested indices;
  incomplete graphs abort before deletion. `acr-retention.test.mjs` verifies
  retention and ordering without external services.
- `acr-retention.mjs` reads hydrated Azure metadata and writes a deletion plan.
- `ghcr-retention.mjs` hydrates upstream descriptors with bounded read concurrency
  and maps the shared plan to GitHub package version IDs. Neither adapter deletes
  artifacts; workflows apply the completed plan afterward.
