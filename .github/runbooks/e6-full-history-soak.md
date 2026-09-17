# E6.3 Full-History AAPL and SPY Soak

This runbook operates the T049 soak: backfilling trades and quotes for AAPL
and SPY over available SIP history through the market-lake ensure path while
recording per-session sessions, rows, bytes, estimated pages, and duration,
and projecting Azure cost from the recorded volume.

The soak driver is `scripts/soak/run_soak.py`; its versioned configuration is
`config/lake/soak-aapl-spy.yaml`. Per-session metrics land in an append-only
JSONL audit file, completed sessions in a resumable state file, and the
aggregate plus cost projection in a summary JSON. All paths default under the
git-ignored `.local/soak/`.

## Design and Measured Behavior

- The driver plans the window with `lake.coverage.equity_sessions` (XNYS
  calendar-aware, closed sessions only) and issues one ensure request per
  symbol, data type, and session, newest first. Per-session requests stay
  inside the dev catalog limit of 366 days.
- Live mode targets the deployed market-lake API through `lakeclient` with
  the bearer token. Dry-run mode performs identical planning and cost math
  with no network access.
- A session is terminal when coverage reports `complete` or `empty`;
  `partial` and `error` sessions are recorded as failures and retried on the
  next invocation.
- Pages are estimated as `ceil(rows / 10000)` because ensure responses do
  not expose provider pagination; 10,000 is the Alpaca v2 historical page
  maximum. This is an estimate, not an observed count.
- Measured launch (2026-09-05, bounded): 8 requests, 8 complete, 8,000 rows,
  95,615 bytes, 16.2 seconds of API duration, roughly 2 to 6 seconds per
  ensure request including Alpaca fetch, Parquet write, and catalog update.

## Cost Model and Price Assumptions

Prices are published Azure list prices for `westus2`, retrieved from the
Azure Retail Prices API (`prices.azure.com`, api-version
`2023-01-01-preview`) on 2026-09-05, and are recorded in
`config/lake/soak-aapl-spy.yaml`:

| Resource | Meter | Price |
| --- | --- | --- |
| Blob (General Block Blob v2, hierarchical namespace, Hot LRS) | Data stored, first 50 TB | $0.018 /GB-month |
| Blob (hierarchical namespace, Hot LRS) | Write operations | $0.065 /10K |
| Blob (hierarchical namespace, Hot LRS) | Read / other operations | $0.005 /10K |
| Tables (Standard LRS) | Data stored | $0.045 /GB-month |
| Tables (Standard LRS) | Transactions | $0.00036 /10K |
| Queues v2 (LRS) | Class 1 operations | $0.004 /10K |
| Container Apps (Consumption) | vCPU active | $0.000034 /vCPU-second |
| Container Apps (Consumption) | Memory active | $0.000004 /GiB-second |
| Container Apps (Consumption) | Requests | $0.40 /1M |
| Log Analytics (PerGB2018) | Ingestion | $2.30 /GB (first 5 GB/month free) |

The dev storage account `fcaobj3kwwfxxfxu` has `isHnsEnabled=true`, so the
hierarchical-namespace Blob meters apply. Projection semantics: recorded
bytes held for `storage_retention_days` (90) prorated from the monthly
price; one Blob write per completed object; two Table transactions per
terminal session; Container Apps compute from measured API durations at the
deployed container size (0.25 vCPU, 0.5 GiB); queue operations zero because
the dev composition runs ensure inline; environment management fee excluded
as a fixed cost. Re-verify prices before any long soak; update the YAML and
its retrieval date together.

## Prerequisites

1. Python 3.12 environment: `uv sync --python 3.12 --extra dev`.
2. Azure CLI authenticated to the FCA subscription with permission to read
   the Key Vault secret and to update the Container App:
   `az account show` must show the `FCA` subscription.
3. Bearer token access: the runtime secret `api-bearer-token` in vault
   `fca-ml-dev-kv-obj3kww` authenticates every non-health route. Fetch it
   into the process environment only; never print, redirect, or persist it:

   ```powershell
   $env:LAKE_API__BEARER_TOKEN = (az keyvault secret show `
       --vault-name fca-ml-dev-kv-obj3kww --name api-bearer-token --query value -o tsv)
   ```

4. Reachability: the dev API ingress is internal-only at
   `fca-ml-dev-api.internal.lemonhill-3b437822.westus2.azurecontainerapps.io`
   and is not reachable from a workstation. To run from a workstation,
   apply the temporary posture below; to run from inside the environment
   (for example an in-cluster runner), skip it.

## Temporary Ingress Posture (workstation runs only)

This is the same posture used by the T048 smoke. Apply immediately before a
soak invocation and restore immediately after; all non-health routes remain
bearer-protected throughout.

```powershell
az containerapp env update --name fca-ml-dev-cae `
    --resource-group fca-rg-westus2 --public-network-access Enabled
az containerapp ingress enable --name fca-ml-dev-api `
    --resource-group fca-rg-westus2 --type external `
    --target-port 8000 --transport auto --allow-insecure false
# External FQDN: fca-ml-dev-api.lemonhill-3b437822.westus2.azurecontainerapps.io
```

Restore (note: `ingress disable` removes ingress entirely; restore with
`--type internal` to the documented posture, then verify):

```powershell
az containerapp ingress enable --name fca-ml-dev-api `
    --resource-group fca-rg-westus2 --type internal `
    --target-port 8000 --transport auto --allow-insecure false
az containerapp env update --name fca-ml-dev-cae `
    --resource-group fca-rg-westus2 --public-network-access Disabled
```

Verification after restore:

```powershell
az containerapp env show --name fca-ml-dev-cae --resource-group fca-rg-westus2 `
    --query "properties.publicNetworkAccess"   # expect: Disabled
az containerapp show --name fca-ml-dev-api --resource-group fca-rg-westus2 `
    --query "properties.configuration.ingress.external"  # expect: false
az containerapp revision list --name fca-ml-dev-api --resource-group fca-rg-westus2 `
    --query "[?properties.isActive].properties.healthState"  # expect: Healthy
```

A mid-wave Bicep deployment by another operator also restores internal
ingress; if your flip disappears, re-apply it and continue.

## Launch

Plan first (no network), then smoke, then full runs:

```powershell
# 1. Full-window plan and cost projection (proves the plan math).
uv run --extra dev python scripts/soak/run_soak.py `
    --config config/lake/soak-aapl-spy.yaml --dry-run

# 2. Bounded live smoke: the 2 most recent closed sessions, 8 requests.
uv run --extra dev python scripts/soak/run_soak.py `
    --config config/lake/soak-aapl-spy.yaml --base-url https://<api-fqdn> `
    --limit-sessions 2

# 3. Full-history soak. A single invocation loads every pending session in
#    the window; the state file resumes exactly where a previous run
#    stopped, so re-running the same command is always safe.
uv run --extra dev python scripts/soak/run_soak.py `
    --config config/lake/soak-aapl-spy.yaml --base-url https://<api-fqdn>
```

Window overrides: `--start-date`/`--end-date` (ISO dates), `--symbols
AAPL,SPY`, `--limit-sessions N`. Exit code 0 means every
attempted session reached a terminal state; exit code 1 means failures were
recorded (inspect `results.jsonl`, fix, re-run). Full history from the
config default of 2016-01-01 plans 2,684 closed sessions per symbol and data
type (10,736 requests); at the measured 2 to 6 seconds per request expect
roughly 6 to 15 hours of wall time spread across invocations.

## Monitoring

Per-invocation, from the driver output and files:

```powershell
Get-Content .local\soak\summary.json                       # aggregate + cost projection
Get-Content .local\soak\results.jsonl -Tail 5              # per-session audit lines
$state = Get-Content .local\soak\state.json -Raw | ConvertFrom-Json
$state.completed.PSObject.Properties.Name.Count            # completed sessions
```

Deployment-side:

```powershell
az containerapp revision list --name fca-ml-dev-api --resource-group fca-rg-westus2 `
    --query "[?properties.isActive].{name:name, health:properties.healthState}"
az monitor log-analytics query -w <workspace-id> --analytics-query `
    "ContainerAppConsoleLogs_CL | where ContainerAppName_s == 'fca-ml-dev-api' `
    | summarize count() by bin(TimeGenerated, 5m)"
```

Lake-side volume (Entra-authenticated, no account key):

```powershell
az storage blob list --account-name fcaobj3kwwfxxfxu --container-name lake `
    --auth-mode login --prefix "date=" --query "length(@)"        # object count
az storage blob list --account-name fcaobj3kwwfxxfxu --container-name lake `
    --auth-mode login --query "[].properties.contentLength | sum(@)"  # bytes
```

Spend guardrails: the resource-group budgets `fca-ml-dev-budget-40usd` and
`fca-ml-dev-budget-70usd` alert by email; check
`az consumption budget list` and Cost Management for the soak period.

## Interruption and Resume

- The state file is rewritten atomically after every terminal session, so an
  interrupted invocation loses at most in-flight requests.
- Re-running the same command skips every key already in the state file and
  retries only failures (`partial`/`error`) and never-attempted sessions.
- To intentionally retry a failed session, remove its
  `symbol|data_type|session_date` entry from `state.json` first; results of
  past attempts remain in `results.jsonl` for audit.
- If the API is unreachable mid-run, the driver exits 1 with completed
  sessions persisted; fix reachability (posture, auth) and re-run.

## Handoff to T050 (Cost Projection)

Hand T050: `summary.json` (aggregate rows, bytes, estimated pages, durations,
and the line-item projection), `results.jsonl` (per-session audit), the
state file entry count, and the price assumptions block from
`config/lake/soak-aapl-spy.yaml` including its retrieval date.

Known fidelity limits for T050 to carry forward: the ingest fetch is now
paginated internally by the lake until the provider cursor is exhausted rather
than a configurable `LAKE_INGEST_MAX_RECORDS` cap, so recorded rows and
bytes are full-depth. The
`estimate.assumed_bytes_per_session` values in the config are
order-of-magnitude planning numbers and should be
replaced by real full-depth measurements once a full soak runs. If
the soak configuration or price assumptions change, re-run the dry-run
projection rather than amending historical summaries.
