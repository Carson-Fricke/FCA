# Trader Operations

The supervised trader process (`harness trader`) is the deployment boundary
that runs one audited paper or shadow session from boot to clean exit. This
runbook documents how the RTH and crypto trader Container Apps run it, what a
container restart does mid-session, how the global kill switch halts trading,
how to inspect the supervision state, and how to triage the exit codes the
platform observes. It extends `.github/runbooks/h5-quant-smoke.md` for the
deployment and image chain. Never echo secret values in logs, issues, or task
evidence; credential names below are references, never values.

## Deployment

Both traders share the runtime image. `deploy-main.yml` deploys:

- `<name-prefix>-rth-trader`: the scheduled RTH equities trader. The container
  runs `harness trader` (the default command runs one supervised session;
  `--config` defaults to `config/harness/trader.yaml`). It scales from zero on
  the KEDA cron rule `rth-schedule`: timezone `rthScheduleTimezone` (default
  `America/New_York`), start `rthScheduleStart` (`30 9 * * 1-5`), end
  `rthScheduleEnd` (`0 16 * * 1-5`), so replicas exist only inside the RTH
  window. See `infra/modules/container-apps.bicep`.
- `<name-prefix>-crypto-trader`: the optional always-on crypto trader
  (`enableCryptoTrader`), pinned to one replica with no autoscaling rules.

Container environment (bicep `quantBaseEnvironment` plus the trader pair):

| Variable | Value |
| --- | --- |
| `AZURE_CLIENT_ID` | Quant runtime managed-identity client id |
| `LAKE_STORAGE_ACCOUNT_NAME` | Runtime storage account name |
| `HARNESS_STATE_CONTAINER` | `harness-state` snapshot/state container |
| `HARNESS_KILL_SWITCH_TABLE` | `killswitch` table name |
| `HARNESS_ALPACA_API_KEY` / `HARNESS_ALPACA_API_SECRET` | Key Vault references (`alpaca-api-key` / `alpaca-api-secret`) |

The boot composition in `src/harness/live/src/supervision.rs` loads the trader
configuration and resolves one execution job id: the configured `job_id`, then
`HARNESS_TRADER_JOB_ID`, then the deployment's persisted identity
(`current.json`), allocating a fresh `job-<opaque>` value when none is set; a
configured or pinned id that conflicts with the persisted identity fails the
boot. The deployment name names the state root, never the execution identity.
That job id keys the supervision journal and snapshots, and fee deduplication
uses the same identity. Every boot is gated in order: crash-loop ledger,
kill-switch gate, scheduled session. The process exit code is the
`TraderExitCode` of the boot.

Manual start outside the KEDA window: force one replica, then revert.

```powershell
az containerapp update --name <rth-trader-app> --resource-group <resource-group> --min-replicas 1
# observe, then revert
az containerapp update --name <rth-trader-app> --resource-group <resource-group> --min-replicas 0
```

The crypto trader is always-on; restart its revision with
`az containerapp revision restart` instead.

## Restart

Exit codes the platform observes (`TraderExitCode` in
`src/harness/live/src/supervision.rs`):

| Exit | Meaning | Platform behavior |
| --- | --- | --- |
| 0 | Clean: session completed/at close, operator stop, or no scheduled session | Deliberate completion |
| 41 | Kill switch engaged at boot or mid-session | Deliberate halt |
| 42 | Boot policy halted (recovery taxonomy refused to resume) | Deliberate halt |
| 75 | Restartable fault (gate poll, connect, market-wait, recovery) | Restart |
| 86 | Crash loop: restart budget spent on the durable ledger | Refuses to boot |
| 1 | Unexpected supervision fault | Fail-closed |

Inside the KEDA window the platform restarts exit-75 replicas with backoff;
each restart re-gates the boot. Outside the window the app scales to zero and
the next cron start boots fresh.

The `resume_reconcile` boot policy restores interrupted sessions: the
supervisor loads `runs/<job_id>/latest.json` from the `harness-state`
container, rebuilds books, marks, and order attribution through
`RuntimeStateStore.restore_shell`, reconciles the restored hub against the
snapshot-derived broker state, and continues dispatch only on a proceeding
decision. Observe the restore through the supervision document: the `phase`
walks `boot`, `gate`, `restore`, `connect`, `running`, `teardown`, and the
`recovery` block records `policy`, `action`, `report_id`, and `snapshot_id`.

## Kill

The global kill switch is one Azure Table row. Contract
(`TableKillSwitchPoller`): table named by `HARNESS_KILL_SWITCH_TABLE` on the
`LAKE_STORAGE_ACCOUNT_NAME` storage account, entity
`PartitionKey='killswitch'`, `RowKey='global'`, boolean `engaged` property and
optional `reason`. A missing row is a disengaged switch. Credentials resolve
from `FCA_KILL_SWITCH_TABLE__ENDPOINT` + `FCA_KILL_SWITCH_TABLE__KEY`
(shared key; local and test environments) or, by default, managed-identity
reads against `LAKE_STORAGE_ACCOUNT_NAME` (with optional `AZURE_CLIENT_ID`).

Engage and disengage (the row replace carries both properties):

```powershell
az storage entity insert --table-name <killswitch-table> --account-name <storage-account> --auth-mode login --if-exists replace --entity PartitionKey=killswitch RowKey=global engaged=true reason="<operator reason>"
az storage entity insert --table-name <killswitch-table> --account-name <storage-account> --auth-mode login --if-exists replace --entity PartitionKey=killswitch RowKey=global engaged=false
```

Verify the switch state:

```powershell
az storage entity query --table-name <killswitch-table> --account-name <storage-account> --auth-mode login --filter "PartitionKey eq 'killswitch' and RowKey eq 'global'"
```

Verify the trader halted: the running session trips its in-process switch at
the next poll (cadence `HARNESS_TRADER_KILL_SWITCH_POLL_SECONDS`), saves a
stop snapshot, and exits `41`; a boot with the switch engaged exits `41`
before any feed is composed. Confirm with the supervision document
(`phase` = `halted`, detail carries the switch reason) and container logs:

```powershell
az containerapp logs show --name <rth-trader-app> --resource-group <resource-group> --tail 200
```

Repeated poll failures are fail-closed: the boot gate turns the first error
into exit `75`, and a running session exits `75` after
`HARNESS_TRADER_KILL_SWITCH_STALE` consecutive failures.

## State inspection

`harness trader status` prints the current job's supervision document. It
resolves the job id the same way the boot does: the configured `job_id`, then
`HARNESS_TRADER_JOB_ID`, then the deployment's persisted identity. Exit `1`
with `no supervision record for <config path>` when absent.

```powershell
$env:HARNESS_TRADER_JOB_ID = "<job-id>"
harness trader status --config config/harness/trader.yaml
```

Fetch the durable documents directly from the `harness-state` container:

```powershell
az storage blob download --container-name harness-state --account-name <storage-account> --auth-mode login --name "runs/<job_id>/supervision.json" --file <supervision.json>
az storage blob download --container-name harness-state --account-name <storage-account> --auth-mode login --name "runs/<job_id>/restarts.json" --file <restarts.json>
```

How to read `supervision.json`:

| Field | Meaning |
| --- | --- |
| `phase` / `detail` | Current supervision phase and its last transition detail |
| `phase_at` / `updated_at` / `last_heartbeat_at` | Clock stamps for the phase, the document, and the last heartbeat |
| `session_status` / `session_reason` | Terminal or in-flight session status from the shell |
| `booted_at` / `last_event_time` | Session boot stamp and the dispatch watermark |
| `market_event_count` / `dispatched_count` / `dispatch_hash` | Progress and determinism watermark |
| `books` | Per-book summary: `book_id`, `initial_cash`, `cash`, `equity`, open `positions` |
| `recovery` | Boot-policy outcome: `policy`, `action`, `report_id`, `snapshot_id`, `failure` |
| `consecutive_faults` | Current restart-ledger counter folded into the status document |

`restarts.json` carries the durable crash-loop budget:
`consecutive_faults` plus the last eight `{at, outcome, detail}` history
entries (`clean`, `kill_switch`, `policy_halt`, `fault`).

## Common Failures

| Symptom | Exit | First checks | Remediation |
| --- | --- | --- | --- |
| `restartable fault: kill-switch gate poll failed` | 75 | Table exists; runtime identity has table data access; `HARNESS_KILL_SWITCH_TABLE` matches | Fix table access; the platform restarts and the next boot re-gates |
| `restartable fault: market-data boundary could not be composed` | 75 | Alpaca Key Vault references resolve; `market_data.adapter` and `feed` setting match the trader | Fix credentials/configuration; restart the revision |
| `market-wait exceeded: no market events by <timestamp>` | 75 | Market session open (RTH window); feed connected; symbols subscribed | Fix feed/universe; widen `HARNESS_TRADER_MARKET_WAIT_MINUTES` only with a recorded reason |
| `restart budget exhausted; refusing to boot` | 86 | `restarts.json` shows `consecutive_faults` >= `HARNESS_TRADER_RESTART_BUDGET` | Fix the root fault first, then clear the ledger by deleting `runs/<job_id>/restarts.json` (below). A clean boot or a kill-switch outcome resets the counter; a spent budget refuses to boot before any other gate, so it cannot clear itself |
| `boot policy <policy> halted: <failure>` | 42 | `recovery.failure` in `supervision.json`; reconcile diff | Resolve the divergence the policy reported, then clear the ledger if a fault was recorded |
| Unexpected supervision fault | 1 | `supervision.json` `detail` and container logs | Treat as a supervision defect; capture the detail before restarting |

Clear a spent crash-loop ledger after the root cause is fixed:

```powershell
az storage blob delete --container-name harness-state --account-name <storage-account> --auth-mode login --name "runs/<job_id>/restarts.json"
```

Audit records live under the job's prefix in the `harness-state` container:
`runs/<job_id>/supervision.json` (status document), `runs/<job_id>/restarts.json`
(restart ledger), and `runs/<job_id>/snapshots/` with the `latest.json`
pointer (recovery restores from these). The kill-switch table itself is the
audit record for halts; keep `reason` human-readable and free of secret
values.
