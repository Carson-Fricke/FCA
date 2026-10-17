# Lake Rust Phases 5–6 — Handoff Instructions

<!-- markdownlint-disable MD013 -->

## Where this work lives

* **Worktree**: `C:\Users\csfri\AppData\Local\Temp\opencode\fca-rust\ws-lake`
* **Branch**: `rust/lake-migration` (created from `master` at `1c046f6`)
* **Design of record**: `.github/architecture/lake-rust-rewrite-plan.md`
  (phases 5 and 6 are defined there; the "Implementation status (phases 0–4)"
  section records what is done)
* **Commit state**: phases 0–4 are **uncommitted** in this worktree. First
  action: confirm gates, then commit the baseline (recommended message:
  `feat(lake): phases 0-4 Rust lake service with flipped CLI`). Do not squash
  the phase-0 oracle fixtures or `Cargo.lock` out of the commit.

## First actions (do these before new work)

1. Verify the baseline in the worktree:

   ```powershell
   # from src\lake
   cargo fmt --all -- --check
   cargo clippy --workspace --all-targets --offline -- -D warnings
   cargo test --workspace --offline
   # from the worktree root, Python gates (reuse the main checkout venv):
   $env:UV_PROJECT_ENVIRONMENT = "C:\Users\csfri\source\repos\FCA\.venv"
   uv run --no-sync --extra dev ruff check . ; uv run --no-sync --extra dev ruff format --check .
   uv run --no-sync --extra dev pytest -p no:cacheprovider tests/lake tests/lakeclient tests/harness/data tests/thesis/packaging/test_image_contract.py -q
   ```

   Expected: Rust green (53 test binaries), Python 690 passed. `ty check`
   reports 94 diagnostics in `tests/thesis/**` at HEAD; those are pre-existing
   and are fixed by the operator's uncommitted edits in the main checkout —
   do not chase them, and do not copy those edits into this branch.
2. Read the plan's "The public contract to preserve" and "The Python surface
   to retain" sections. The frozen surfaces are parquet layout/schemas, the
   HTTP contract, the console script, and the retained Python imports.
3. Read `src/lake/providers/src/lane.rs` and `src/lake/providers/src/client.rs`
   once. The adaptive fetch lane and the split-on-cursor planner are load
   bearing; the "Known pitfalls" section below explains what must not regress.

## What phases 0–4 delivered (state of the repo)

* Rust workspace at `src/lake/` (crates: `contracts`, `configuration`,
  `catalog`, `coverage`, `providers`, `storage`, `ingestion`, `query`, `http`,
  `local`, `cli`), gated by `cargo fmt/clippy/test`.
* Published routes served by axum: `/health`, `/healthz`, `/v1/auth/check`,
  `/v1/catalog/coverage`, `/v1/catalog/ensure`, `/v1/jobs/{job_id}`,
  `/v1/query` (file mode), `/v1/raw`. Option coverage/selection are
  implemented (`http/src/selection.rs`); SQL mode returns the Python-parity
  `query_unavailable`.
* Local mode: `lake local api|ingest|reset`, local layout + JSON catalog,
  equity and crypto ingest, on-demand ensure via the backfill launcher,
  one shared catalog instance per process (see pitfalls).
* `lake` console script is a thin shim (`src/lake/cli/rust_entrypoints.py`).
* Phase-0 oracle: `src/lake/tests/oracle/generate.py` plus fixtures in each
  crate's `tests/oracle/` (XNYS sessions, catalog bytes, ensure job ids,
  Python-written parquet).
* Config: `provider.request.max_concurrency` is the **adaptive ceiling**
  (default 512, validated `1..=1024`; the Python `settings.py` model was
  raised to match), and `provider.request.requests_per_minute` exists as the
  optional budget.
* Dockerfile builds the `lake` binary in a `lake` stage; CI has the
  `lake-rust` job; `MANIFEST.in`/`.dockerignore` updated.

## Phase 5 status (all workstreams landed and verified)

Phase 5 is complete on this branch. Each workstream landed as its own commit
and the full workspace gates pass (`cargo fmt/clippy/test`: 698 tests across
68 binaries; Python ruff + 784 pytest tests).

* **5A (T351)** — `lake-providers` gained option records/parsing and the
  `/v1beta1/options` fetch path; `lake-ingestion/src/options/` ports the
  selector, transaction, backfill, and queue handle. The e2e coverage-route
  test lives in `ingestion/tests/options_http_end_to_end.rs` (the canonical
  `http/tests/` relocation needs `lake-ingestion`/`lake-storage`/`lake-providers`
  dev-deps in the http crate; deferred to keep the http dep graph one-way).
* **5B (T352)** — new `lake-reference` crate: EDGAR/Nasdaq ingest with the SEC
  10 rps cap and User-Agent enforced by construction, byte-identical schemas,
  and both point-in-time read rules as parity tests against Python-written
  oracle parquet.
* **5C (T353)** — `lake-query` executes bounded SQL through the bundled
  DuckDB; the `data` relation is a materialized temp table (a view would
  re-bind under `enable_external_access = false`), the plan check greps the
  uppercased EXPLAIN text, and `http`/`local` wire the engine through
  `LakeApiState.bounded_query` with `api.sql.*` settings.
* **5D (T355)** — new `lake-queue` crate (worker, renewal cadence,
  dead-letter envelope/replay) plus the `QueueSender` port in `lake-storage`;
  parity-tested against the Python suite's cases.
* **5E (T356)** — official Azure SDK crates 0.21 (`azure_core`, `azure_identity`,
  `azure_storage_blobs`, `azure_data_tables`, `azure_storage_queues`): tables
  catalog/job adapters, blob store/SAS/leases, queue transport, Entra JWT
  validation (`jsonwebtoken` + injectable JWKS fetch), `lake-hosted`
  composition (`LAKE_STORAGE_ACCOUNT_NAME` selects Azure), and the
  `lake ingest`/`lake operations` CLI commands.
* **Thesis reference migration (T354)** — the classification/fundamentals
  query surface moved to `harness.data.reference`; Thesis no longer imports
  `lake.reference` anywhere, so phase 6 can delete the Python reference
  modules instead of retaining them.

### Operator decisions recorded

1. Reference: Thesis never imports `lake.reference` — reads go through the
   harness (done); the Python `lake.reference` modules are phase-6 deletions.
2. Azure access: official SDK crates (used; no REST fallback was needed).
3. Multiple API keys: out of scope for phase 5; the lane is per-key so adding
   lanes is additive.
4. `requests_per_minute`: left unset in the committed YAML; the HOLD path
   arms only when configured.

## Phase 5 workstreams

Run workstreams as separate agents with disjoint file ownership. The
integration owner (the session running these instructions) wires, verifies
repo-wide, and updates docs.

### 5A — Options ingestion and selection wiring

* Sources: `src/lake/ingestion/options/{selector,backfill,transaction,worker}.py`
  and `tests/lake/ingestion/options/**`.
* Target: `src/lake/ingestion/src/options.rs` (or `options/`), selector
  helpers in the same crate; front sets come from `lake_catalog`
  (`ContractRepository`, `DailyFrontSetRepository`).
* Semantics to preserve exactly: OSI contract symbols as partitions, missing
  or empty closed sessions still write valid empty parquet with terminal
  `empty` coverage, the transaction error shape, nearest-expiration and
  spot-strike-window selection determinism, front-set persistence shape.
* Composition parity: the Python local launcher **rejects** options and the
  Python hosted runtime is equity-only; do not invent option-ensure wiring.
  Option ingest is reached through the queue worker and CLI paths, exactly as
  in Python.
* Exit: Rust tests for selector/transaction/queue handle; option parquet
  decodes against `contracts/tests/oracle` schemas; HTTP option coverage
  (already implemented) is covered by an http-crate test that exercises a
  real option transaction end to end.

### 5B — Reference data

* Sources: `src/lake/reference/**` (configuration, http, edgar, nasdaq,
  ingest, partitions, schemas, query) and `tests/lake/reference/**`.
* Target: new crate `src/lake/reference/` (workspace member), depending on
  `storage` (blob store) and `contracts`.
* **Retention decision**: `src/thesis/screening/context.py` imports
  `lake.reference.query.classification_snapshot` / `latest_fundamentals`, and
  thesis tests import `lake.reference.schemas` —
  retain those Python modules until Thesis migrates. Port the ingest
  (EDGAR/Nasdaq fetch, rate limiter, classification/fundamentals partitions)
  to Rust; keep the Python `query.py`/`schemas.py` (and their transitive
  imports) as part of the retained surface.
* Exit: reference partitions written by Rust decode identically to the
  Python fixtures; the SEC 10 rps cap and User-Agent requirements are
  enforced; retained Python imports still resolve.

### 5C — DuckDB-backed SQL query

* Sources: `src/lake/query/service.py` (bounded query), the SQL half of
  `src/lake/query/application.py`, and `tests/lake/query/test_bounded_query.py`.
* Target: `src/lake/query` behind the existing `FileQueryContext`; replace
  the `query_unavailable` stub in `http/src/routes/query.rs` with the real
  engine.
* Engine choice: the `duckdb` crate (bundled) — network is available; do not
  use `--offline` when adding it. Expect a long first build; that is fine.
* Semantics to preserve exactly: read-only SQL validation and rejection
  codes, only ensured file paths registered, scan/result byte limits,
  `force` + authorization (`force_override_not_authorized`), the arrow-stream
  output headers, decimal text rendering, and the 413/403/422/503 mappings.
* Exit: port `tests/lake/query/test_bounded_query.py` cases as Rust tests and
  cover the HTTP layer in `http/tests`.

### 5D — Queue worker and dead letters

* Sources: `src/lake/queue/{worker,dead_letter,adapters}.py`,
  `tests/lake/queue/**`.
* Target: `src/lake/storage` gains the `QueueSender` port and
  `src/lake/queue` becomes a crate (workspace member) owning the worker,
  visibility renewal, retry/dead-letter policy from `QueueSettings`, and the
  dead-letter envelope/replay shapes.
* Exit: parity tests for delivery counts, renewal cadence, exhaustion →
  dead-letter, and replay; Azure adapters arrive in 5E.

### 5E — Azure hosted boundaries

* Sources: `src/lake/hosted/runtime.py`, `src/lake/catalog/azure.py`,
  `src/lake/storage/azure.py`, `src/lake/ingestion/azure.py`,
  `src/lake/queue/adapters.py`, `src/lake/http/auth.py` (Entra),
  `tests/lake/hosted/**`, `tests/lake/http/test_entra_auth.py`.
* Target: implementations of the existing Rust ports — `CatalogRepository` /
  `JobRepository` over Azure Tables, `BlobStore`/`ReadableBlobStore` over
  Blobs, `QueueSender` over Queues, `SasIssuer` user-delegation SAS,
  `LeaseProvider` over blob leases — plus a `lake-hosted` composition crate
  wiring `lake api` and `lake ingest`.
* Dependency decision to confirm with the operator: official Azure Rust
  crates (`azure_core`, `azure_identity`, `azure_storage_blobs`,
  `azure_data_tables`, queue crate) vs REST + `azure_identity` tokens. Prefer
  SDK crates; fall back to REST only if a needed surface is missing. Entra
  JWT: `jsonwebtoken` + JWKS fetch. Network is available; commit
  `Cargo.lock` updates.
* CLI: restore `lake ingest` (Azure single-session) and `lake operations`
  (daily report) with the Python flags, environment names, and exit codes.
* Exit: hosted tests adapted from the Python suite; `lake api` selects the
  Azure composition when `LAKE_STORAGE_ACCOUNT_NAME` is present and the
  in-memory one otherwise (Python `hosted_overrides()` behavior); Entra
  accepted/rejected cases match `test_entra_auth.py`.

## Phase 6 — delete the Python lake

**Status: complete.** The service-shaped Python modules are deleted
(`local/`, `http/`, `ingestion/`, `storage/`, `query/`, `queue/`, `hosted/`,
`operations/`, `reference/`, `tools/`, `catalog/azure.py`,
`coverage/{service,application}.py`, `app.py`, `crypto.py`, `options.py`),
keeping only the retained surface; the coverage package now re-exports the
session helpers from a reduced `lake/coverage/sessions.py`. The catalog test
files kept only their in-memory/model cases (the Azure-adapter cases moved
with the Rust crate), the CLI facade test imports only the retained facades,
and the EDGAR/Nasdaq golden fixtures moved into
`src/lake/reference/tests/fixtures/`. Only start this section's work when
every phase-5 workstream is green and the harness and Thesis suites pass
against the Rust lake — both held.

### Retained Python surface (verify the import closure before deleting anything)

| Consumer | Must resolve |
| --- | --- |
| `scripts/soak/run_soak.py` | `lake.configuration.loader`, `lake.coverage.equity_sessions` |
| `scripts/experiments/spy_0dte/*` | `lake.contracts.*`, `lake.configuration`, `lake.providers.alpaca.*`, `lake.catalog.models` |
| `strategies/harness_examples/short_put_spread.py` | `lake.catalog.models.ContractRecord` |
| `tests/harness/data/*` | `lake.catalog.front_sets`, `lake.catalog.memory`, `lake.catalog.models` |
| everyone | `lake` console script (`lake.cli:main`), `python -m lake`, `lakeclient` (untouched) |

Keep: `lake/contracts/` (py), `lake/configuration/` (loader/settings/universes),
`lake/coverage/` reduced to the session helpers (re-export from contracts is
acceptable if Python consumers only need `equity_sessions` / `crypto_utc_days`),
`lake/catalog/{models,front_sets,memory}.py` plus whatever those import,
`lake/providers/alpaca/`, `lake/cli/` (shim only), `lake/__init__.py`,
`lake/__main__.py`. Thesis migrated off `lake.reference` in T354: the query
surface now lives in `harness.data.reference`, so `lake/reference/` is deleted
in full.

### Delete

`lake/local/`, `lake/http/`, `lake/ingestion/`, `lake/storage/`,
`lake/query/`, `lake/queue/`, `lake/hosted/`, `lake/operations/`,
`lake/reference/`, `lake/tools/`, `lake/catalog/azure.py` (and its `__init__` exports),
`lake/coverage/{service,application}.py`, `lake/app.py`, `lake/crypto.py`,
`lake/options.py`, and any Python files left in Rust-owned directories that
are not in the retained table.

**Directory collision warning**: the Rust crates live in same-named Python
package directories (`src/lake/local`, `http`, `ingestion`, `storage`,
`query`, `catalog`, `coverage`, `configuration`, `contracts`, `providers`,
`cli`). Delete only `*.py` and `__pycache__` inside those; never delete
`Cargo.toml`, `src/`, or `tests/`.

### Tests and docs

* Delete Python tests that cover only deleted modules; keep and adapt
  `tests/lake/contracts`, `tests/lake/configuration`, the catalog
  model/front-set/memory tests, `tests/lakeclient`, `tests/harness/data`,
  `tests/thesis/screening`. `tests/lake/cli` was already rewritten for the
  shim.
* Run the consumer smoke: `uv run lake --help`, `python -m lake --help`, and
  a scripted import of every symbol in the retained table.
* Update `.github/architecture/repository.md` (delete the "phase 5 not yet
  ported" notes, state the final shape), the rewrite plan (mark phases 5–6
  complete), and any runbook that references deleted modules.
* Check `MANIFEST.in`, `pyproject.toml`, and the Dockerfile for references to
  deleted paths; the image's lake build stage and `LAKE_RUST_BINARY_DIR` stay.

## Conventions and gates (non-negotiable)

* No `#[cfg(test)] mod tests` in `src/**/*.rs`; unit tests needing private
  access go in `tests/unit/<name>.rs` via `#[path]`, integration tests in
  `tests/<name>.rs`. Mirror the module name.
* Never assign the same file to concurrent agents; integration owns
  repo-wide verification and runs it once after edits freeze.
* `ruff` and `ty` only (no other Python lint/type tools); Rust gates are
  `cargo fmt --all -- --check`, `cargo clippy --workspace --all-targets
  -- -D warnings`, `cargo test --workspace`.
* Frozen: parquet layout/schemas/metadata, HTTP paths/payload field names and
  status codes, option-selection error codes, `ensure-<sha256>` job ids,
  Hive paths including `%2F`-encoded crypto pairs, local `catalog.json`
  bytes. Do not add shims or adapter layers to avoid touching call sites;
  change the real code path.

## Known pitfalls (each one cost real debugging time)

1. **The provider is the throughput wall, not our bandwidth or the budget.**
   Measured ~15–40 page-requests/s per key, no 429s observed at any
   concurrency; latency grows when multiple session streams interleave.
   Never reintroduce unbounded fan-out, latency-skew backoff, or periodic
   probe storms in `lane.rs`; the current law (ramp ×2 → settle at
   `1.25 × best_rate × min-latency` → saturation guard at 10 s → governor
   HOLD) was tuned against measurements. Lane constants are documented in
   the file.
2. **Keep `http1_only()` and the explicit `Accept-Encoding: gzip, br`.**
   HTTP/2 multiplexed every request through one connection and collapsed to
   ~1.6 MB/s (10–28 s per page); reqwest strips the encoding header after
   decoding, so absence of the header in logs does not mean no compression.
3. **Split-on-cursor exactness**: boundaries are nanosecond-exact and the
   page's trailing same-timestamp run is deliberately dropped and re-fetched
   by the children. Changing the planner without preserving this loses rows;
   the regression test is
   `a_cursor_split_never_loses_rows_tied_with_the_page_boundary`.
4. **Current coverage authority.** The historical mutable JSON catalog is
   retired. Current local/Azure compositions use `PublicationCatalog` and
   fenced symbol-day manifests as documented in `market-data-runtime.md`.
   Readers do not keep an independent mutable coverage cache.
5. **Windows file locking**: stop any running `lake.exe` before
   `cargo build --release`; stale servers hold the binary and old trace/log
   handles (`Stop-Process -Name lake -Force`).
6. **Do not run two `cargo test --workspace` invocations concurrently**;
   they lock the target dir and produce misleading partial output.
7. **Dev scaffolding to remove before merge**: the `scripts/*.ps1` RCA smoke
   scripts (`rca-*`, `*-probe*`, `live-*`, `week-run.ps1`,
   `one-trade-session.ps1`) are session artifacts; delete or relocate them.
   Keep `src/lake/tests/oracle/generate.py` and its fixtures.
8. **`cargo --offline` was self-imposed for phases 0–4.** New dependencies
   (duckdb, Azure, jsonwebtoken) require a normal `cargo fetch`; the machine
   has network access and the crates cache will absorb most versions already
   present in `Cargo.lock`.
9. **Config semantics changed deliberately**: `max_concurrency` is a ceiling,
   not a fixed bound (default 512). Python `lake/configuration/settings.py`
   was raised to `1..=1024` so both stacks read the same YAML.

## Open decisions for the operator

1. Reference: keep the Python `lake.reference.query`/`schemas` for Thesis
   indefinitely, or schedule a Thesis migration and then delete them?
2. Azure access: SDK crates first (recommended) or REST-only?
3. Multiple API keys for horizontal throughput — out of scope for phase 5,
   but the lane is already per-key, so adding lanes is additive when keys
   exist.
4. Keep `requests_per_minute` configured in the committed YAML, or leave it
   unset (the HOLD path only arms when it is configured)?

## Acceptance criteria (from the plan, restated as commands)

* Coverage responses and parquet partitions byte-identical to the phase-0
  oracle: `cargo test --workspace` (oracle tests) + one live
  `lake local ingest` smoke.
* Throughput: measured and reported, not asserted; the adaptive lane is the
  mechanism. One SPY quote session ≈ 4–5 s end to end, exact rows; one SPY
  trade session ≈ 4 s fetch.
* Warm coverage query under 20 ms at 30k rows: covered by the indexed store;
  add a benchmark only if it becomes a question.
* Harness and Thesis suites pass unchanged against the Rust lake:
  `pytest tests/harness tests/thesis` with the Rust binary on
  `LAKE_API_URL`/`LAKE_RUST_BINARY_DIR` where the suite drives the local
  API.
* Console script and retained imports resolve: `uv run lake --help` and the
  import-closure script from phase 6.
