# Partitioned ingestion and build performance: source verification

## Scope

Replace the ACI executor with finite ACA Job executions, partition long plans
under one public job ID, use the same planner with local child processes, enforce
the Alpaca allowance across each environment, remove intermediate row JSON,
overlap CPU staging with acquisition, and improve reader/build/JIT cache reuse.
The allocations remain 4 vCPU / 8 GiB per ingestion execution and 1 vCPU / 2 GiB
for the main API. Defaults are eight workers, two sessions per worker and
seven-day date partitions. Local and Azure must not ingest simultaneously using
the same Alpaca allowance, per the user's explicit execution policy.

This record verifies source and local checks. It does not assert that the new
images have been deployed or that GitHub-hosted verification has run.

## Behavioral evidence

- `src/lake/ingest-jobs/tests/unit/azure_http.rs`: one public job across disjoint
  partitions, bounded fleet admission across jobs, duplicate admission/conflict,
  immutable accepted executor configuration, durable dispatch intent before
  start, unknown-start reconciliation without replay, stale ownership fencing,
  heartbeat cancellation and aggregate completion. Vendor-shaped minimal 200
  acknowledgements and empty 202 operations are followed by exact execution GETs;
  mismatched identities/templates/images and unsafe polling locations fail.
- Request budget tests across providers, jobs, local persistence and worker:
  every attempt/retry consumes admission, Blob CAS conflicts and lost
  acknowledgements cannot refund permits, storage HTTP Date supplies Azure time,
  local processes share OS uptime, reboot recovery holds capacity, and stalled
  RPCs cannot extend monotonic worker permit lifetimes.
- `src/lake/ingest-worker/tests/local.rs`: real concurrent finite processes over
  fake provider HTTP, one retained public ID, trades/quotes/bars publication,
  durable ownership, local HTTP admission and native/HTTP output parity.
- Ingestion transaction tests retain integrity and failure behavior; typed Arrow
  builders cover all trade/quote schemas and bars with exact decimal values.
- `src/harness/market-reader/tests/snapshots.rs`: snapshot/cache integrity,
  coalescing/restart reuse, bounded oversized streaming and ordered prefetch.
  Objects above 64 MiB compressed are not speculatively fetched.
- JIT behavior still performs real compilation, loading, ABI/provenance checks,
  dependency invalidation and concurrent generation validation. Shared dependency
  targets are locked; returned libraries have immutable generation paths.

## Integration checks

All commands ran from the repository root. One initial pass and one final pass
followed the ACA acknowledgement repair; no further full-suite reruns occurred.

| Check | Result |
| --- | --- |
| `uv lock --check` | Passed |
| `uv run --extra dev ruff check .` | Passed |
| `uv run --extra dev ruff format --check .` | Passed, 740 files |
| `uv run --extra dev ty check` | Passed |
| `uv run --extra dev pytest -p no:cacheprovider --durations=15` | 1,675 passed, one opt-in live Cosmos test skipped; final 180.10 seconds |
| `cargo fmt --manifest-path src/lake/Cargo.toml --all --check` | Passed |
| `cargo clippy --manifest-path src/lake/Cargo.toml --workspace --all-targets --locked -- -D warnings` | Passed |
| `cargo test --manifest-path src/lake/Cargo.toml --workspace --locked` | 115 passed |
| `cargo fmt --manifest-path src/harness/Cargo.toml --all --check` | Passed |
| `cargo clippy --manifest-path src/harness/Cargo.toml --workspace --all-targets --locked -- -D warnings` | Passed |
| `cargo test --manifest-path src/harness/Cargo.toml --workspace --locked` | 318 passed; final fmt/lint/test commands together 160.47 seconds |
| `npm --prefix src/ui run check` | Passed |
| `npm --prefix src/ui test` | 280 passed, one skipped; 41.09 seconds |
| `npm --prefix src/ui run build` | Passed |
| `node --test scripts/ci/changes.test.mjs` | Six passed |
| Bicep build/lint and Azure deployment validation | Passed; existing nullable/unused-parameter warnings remain |
| Actionlint 1.7.12 over all eight changed workflows | Passed with only the old validator's unsupported `queue` key diagnostic excluded |
| `git diff --check` | Passed |

Python's final run narrowly exceeded the hard three-minute target by 0.10 seconds
while native/image builds were active; the initial loaded run took 201.96 seconds.
This record does not claim the timing ceiling was met. The harness's first cold
pass took 931.77 seconds including a 534-second DuckDB/Clippy build and a
197-second test compilation; its final warm pass is the measurement above.
Raw logs are retained in ignored `.local/verification/aca-fleet/`.

## Measured CPU improvement

Three offline optimized replays of the retained MSFT 2025-12-30 records generated
the exact original Parquet bytes/checksums. No external provider was contacted.

| Dataset | Rows | Previous staging sample | New staging range |
| --- | ---: | ---: | ---: |
| Trades | 367,988 | 0.893 s | 0.161–0.162 s |
| Quotes | 201,630 | 0.674 s | 0.098–0.105 s |

The earlier timing is one sample, and the new range is three samples. These are
CPU staging gains, approximately 5.5x for trades and 6.4–6.9x for quotes, not a
measurement of fleet/network throughput. Replay evidence remains in ignored
`.local/columnar-profile/replay.log`.

## Build evidence and deployment prerequisites

Canonical and filtered Python wheels had the same 429 entries and matching
contents excluding wheel RECORD; installed SDK smoke passed in both builders.
The complete Thesis runtime smoke passed as UID 10001, and its fully warm rebuild
took 5.87 seconds. Production recipes omit test targets without changing lockfile
semantics. The finite worker dependency tree excludes DuckDB.

Azure accepted the current ARM shape with no validation error. Operator bootstrap
`aca-fleet-bootstrap-20261015` succeeded, updating the conditioned deployer's
ACA Jobs role allow-list. Runtime images and control-data cutover are separate
operational steps. Old job documents must be drained/deleted before current
writers; canonical market objects and coverage do not require conversion.

At source verification time the latest GitHub attempts remained blocked before
runner startup by the account payment/spending-limit error. Build caches and
workflow selection do not remove that account-level block.
