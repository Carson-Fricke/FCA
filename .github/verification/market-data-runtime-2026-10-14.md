# Harness market-data runtime verification

Source baseline: `c92c355132d371f40a06493459d664e284e7eadc`.
This record describes the local implementation, not an Azure deployment.
The current boundaries and cutover are in
[`market-data-runtime.md`](../architecture/market-data-runtime.md).

## Source and behavior

- `uv lock --check`, repository Ruff lint/format and `ty` passed. The subsequent
  build-only `scripts/ci/merge_cargo_recipes.py` addition also passed focused
  Ruff lint/format and `ty`.
- `uv run --extra dev pytest -p no:cacheprovider --durations=15`:
  **1,675 passed, one skipped, 120.58 seconds**. The skipped test requires a live
  Cosmos latency environment. Mandatory delivery and job-identity guards ran.
- Both Rust workspaces passed formatting and workspace/all-target Clippy with
  warnings denied. Final HTTP and reader/client edits passed focused checks.
- Lake workspace: **102 tests passed** in the full run. The final admission suite
  passed **15 tests**, including the added mounted option-resolution check and
  updated ensure acceptance-location behavior.
- Harness API: **68 tests passed**. Reader/client/backtest/marketdata packages:
  **117 tests passed** in their combined run. The final reader/client run also
  passed, including two additional reader behaviors for disabled caches and
  empty-partition eviction; the reader now has five tests.
- Bicep compilation and workstation-launcher PowerShell parsing passed.
  Existing unrelated Bicep warnings remain.
- Changed workflows passed actionlint 1.7.12, excluding its outdated diagnostic
  for the existing `concurrency.queue: max` property. That property is supported
  by [GitHub's current workflow documentation](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).
- `uv build --python 3.12 --wheel --force-pep517 --out-dir
  .local/market-runtime-dist --clear --no-create-gitignore .` and the isolated
  installed SDK smoke passed.

The first Python run overlapped native compilation and missed a two-second
streaming-test deadline. All 11 streaming tests passed in isolation, and the
quiet full rerun above passed within the three-minute budget. The first harness
workspace test build encountered a Windows executable lock, then a DuckDB
artifact-link failure; focused recompilation and the affected package suites
passed afterward. No further full Rust reruns were performed.

## Linux artifacts

- Shared runtime and dedicated finite-ingestion images built successfully.
- The combined cargo-chef skeleton resolved **485 packages under `--locked`**.
  A subsequent source rebuild reused the native dependency cook layer and
  completed in **371.7 seconds**, avoiding another cold DuckDB build.
- Runtime entrypoints (`api`, `harness`, `mcp`, `backtest-job`, `screen-job`,
  `trader`, `lake`) executed their help commands. UID 10001 could write Cargo
  home, and the portable SDK resolved offline.
- Worker and dispatcher entrypoints executed and rejected missing required
  configuration.
- A temporary combined Linux API, with fake configuration and no cloud
  credentials, passed eight HTTP smoke assertions: both health routes, both
  anonymous rejections, both configured credentials, and both crossed-token
  rejections. The temporary container was removed afterward.

The API image includes the currently published screening binary. Cargo home is
owned during `COPY`, avoiding a second ownership-change layer. Backend image
contexts exclude local caches and generated UI dependencies/build output.

## Removed execution paths

Standalone lake API/local provider-ingestion commands, process-owned ensure
execution, the unused queue worker/enqueue contracts, and storage-owned
market-data decoding/Arrow encoding were removed. Their local/CLI acceptance
tests were superseded by current admission, snapshot, worker-control and native
execution behavior tests; no current delivery or execution-identity guard was
removed or weakened.

## Rollout state

Azure resources were inspected only; no release, provider ingestion or cloud
data cutover was performed. `Microsoft.ContainerInstance` is currently
unregistered. Register it and update the conditioned OIDC deployer role grant
before the first main release, following
[`github-azure-oidc-bootstrap.md`](../runbooks/github-azure-oidc-bootstrap.md).
Main must deploy before the first Thesis release using the new host shape.
Real ACI ingestion remains a rollout validation step; fake-Azure behavior tests
and successful images do not prove a live worker execution.

Existing local backends remained running their previous processes; the
implementation did not restart them or switch them to an undeployed endpoint.
