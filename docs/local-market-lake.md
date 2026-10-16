# Run the complete harness locally

`scripts/local/start-stack.ps1` starts the harness, Thesis, UI and Cosmos emulator. The harness owns market-data catalog, availability, snapshots and reading. Finite local worker processes perform ingestion against local storage; no Azure lake or ACA deployment is required.

```powershell
powershell -File scripts/local/start-stack.ps1 -Foreground
```

The launcher builds optimized harness and worker binaries, selects `HARNESS_MARKET_DATA_MODE=local`, configures `LAKE_LOCAL_WORKER_EXECUTABLE` and `LAKE_LOCAL_PROVIDER_ENV`, and preserves stable local API credentials. The ignored provider file supplies `LAKE_PROVIDER__API_KEY` and `LAKE_PROVIDER__API_SECRET`; only the worker consumes provider configuration. `LAKE_LOCAL_DATA_DIR` selects an owned data tree.

To use Azure, point local Thesis/UI at the entire remote harness instead of starting a local harness that depends on a remote lake. Azure uses the same application behavior with cloud storage and ACA worker adapters.

The combined host injects native access directly. Separate clients explicitly configure `LAKE_API_URL` and `LAKE_API__BEARER_TOKEN`; workstation defaults select `http://127.0.0.1:8001/market-data`. Snapshot requests return pinned manifests and scoped download URLs. The shared reader verifies compressed Parquet and reuses bounded caches. It never acquires missing provider data.

`POST /v1/catalog/ensure` returns covered data or a retained job ID. `GET /v1/jobs/<job_id>` exposes progress and terminal outcome. Same-ID/same-input submissions join, different inputs conflict, terminal outcomes remain immutable, and unknown outcomes are reconciled rather than replayed. Local workers pin their executable by content hash and use kernel-owned job/publication locks.

`HARNESS_MARKET_DATA_CACHE_DIR`, `HARNESS_MARKET_DATA_CACHE_DISK_BYTES` and `HARNESS_MARKET_DATA_CACHE_MEMORY_BYTES` configure retained caches. `LAKE_INGEST_MAX_WORKERS`, `LAKE_INGEST_PARTITION_DAYS`, `LAKE_INGEST_SESSION_CONCURRENCY` and `LAKE_INGEST_TIMEOUT_SECONDS` configure the worker fleet, calendar partitions, session pipeline and execution deadline. Azure defaults to eight finite ACA Job executions at 4 vCPU / 8 GiB, 31-day partitions and two sessions per worker; the main API remains 1 vCPU / 2 GiB. Local worker count defaults to half the available CPU threads, capped at eight. Smaller date partitions favor short-plan fan-out; larger partitions amortize worker startup for bulk history. Accepted jobs retain their original configuration.

One shared reservation ledger enforces 10,000 provider requests/minute across each environment's workers, including retries. Idle workers do not reserve a fixed rate share. Local and Azure must not ingest simultaneously with the same Alpaca allowance; local ingestion remains independent of Azure.

`market-data-profile --session <closed-date>` benchmarks native acquisition and verified reads. `--endpoint <combined-base>/market-data` measures a separate client. Use one closed symbol-session and isolated storage for cold ingestion comparisons; `--no-ensure` measures existing data. Reports never contain credentials or signed URLs.

`uv run --extra dev lake local reset --yes` explicitly removes only a marker-valid local data tree. Outdated documents are rejected, never upgraded at read time. See `.github/architecture/market-data-runtime.md` for boundaries and deployment details.

The shared Linux image defaults to `/app/.local/market-lake` and its bundled finite worker. Set `LAKE_LOCAL_PUBLIC_URL` to the externally reachable combined market-data base when forwarding ports or serving LAN clients; it controls scoped object-download URLs, independently of the bind address.
