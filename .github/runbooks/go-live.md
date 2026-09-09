# Go-Live Ramp, Rollback, and Alerts

The supervised trader (`harness trader`) exits the rehearsal boundary through a
stepped size ramp, not a switch flip. This runbook documents the go-live path
for the RTH and crypto trader Container Apps: the preflight checklist, the
kill-switch drill, the stepped risk-size ladder, the halt criteria, the
rollback procedure, and the alert surface. It extends
`.github/runbooks/trader-operations.md` for the deployment, exit codes, kill
switch, and supervision documents; that runbook extends
`.github/runbooks/h5-quant-smoke.md` for the deployment and image chain. The
alert infrastructure lives in `infra/modules/monitoring.bicep`. Never echo
secret values in logs, issues, or task evidence; credential names below are
references, never values.

## Alert Deployment

`infra/modules/monitoring.bicep` composes the trader alert surface on the
existing Log Analytics workspace (`fca-ml-dev-logs` in dev): one email action
group, two Log Analytics query alerts over the trader console logs, and one
activity-log alert on trader management operations. The module is not yet
wired into `infra/main.bicep`; deploy it standalone and wire it with the
module reference from the go-live task report when integration lands.

```powershell
az deployment group create `
  --resource-group <resource-group> `
  --template-file infra/modules/monitoring.bicep `
  --parameters namePrefix=fca-ml-dev logAnalyticsWorkspaceName=fca-ml-dev-logs traderAlertContactEmail=<operator-email>
```

## Preflight Checklist

Run in order against dev before any ramp step. Record every command and its
non-secret output in the drill record. The storage account and app names
derive from the deployment (`az storage account list --resource-group
<resource-group> --query "[?tags.product=='market-lake'].name | [0]"`).

- [ ] **Secrets present, values never read.** Verify existence and enabled
      state only; the query prints names, never values (see
      `.github/runbooks/key-vault-runtime-secrets.md`):

  ```powershell
  az keyvault secret list --vault-name fca-ml-dev-kv-obj3kww --query "[].{name:name, enabled:attributes.enabled}" --output table
  az keyvault secret show --vault-name fca-ml-dev-kv-obj3kww --name alpaca-api-key --query name --output tsv
  az keyvault secret show --vault-name fca-ml-dev-kv-obj3kww --name alpaca-api-secret --query name --output tsv
  ```

- [ ] **Kill-switch table present and disengaged.** Presence is a management
      plane check (the same `az rest` pattern the smoke uses in
      `.github/workflows/quant-smoke.yml`; the GitHub deployer identity holds
      no storage data-plane roles by design). Disengagement is a data-plane
      check: a missing row is a disengaged switch
      (`TableKillSwitchPoller` contract in `src/harness/cli/supervisor.py`).

  ```powershell
  $arm = "https://management.azure.com/subscriptions/<subscription-id>/resourceGroups/<resource-group>/providers/Microsoft.Storage"
  az rest --method GET --url "$arm/storageAccounts/<storage-account>/tableServices/default/tables/killswitch?api-version=2023-05-01" --query name --output tsv
  az storage entity query --table-name killswitch --account-name <storage-account> --auth-mode login --filter "PartitionKey eq 'killswitch' and RowKey eq 'global'"
  ```

- [ ] **Kill-switch drill PASSED** (details below; run this before the first
      ramp session and re-record after any trader redeploy).

- [ ] **Supervision documents clean.** Fetch the latest run documents and
      confirm: `phase` is `completed`, `at_close`, or a healthy `running`
      heartbeat; no `faulted`/`halted` residue; `consecutive_faults` is `0`;
      the `recovery` block is absent or carries `action` != `halt` and no
      detail beginning `reconciliation aborted` (field meanings in
      `.github/runbooks/trader-operations.md`):

  ```powershell
  az storage blob download --container-name harness-state --account-name <storage-account> --auth-mode login --name "runs/<run-id>/supervision.json" --file supervision.json
  az storage blob download --container-name harness-state --account-name <storage-account> --auth-mode login --name "runs/<run-id>/restarts.json" --file restarts.json
  ```

- [ ] **Restart ledger empty or budget-clear.** `restarts.json`
      `consecutive_faults` must be below `HARNESS_TRADER_RESTART_BUDGET`
      (default 3) and the `history` tail must be `clean` or `kill_switch`
      outcomes. A spent budget exits `86` before any gate; clear it only
      after the root fault is fixed (see the restart-loop section).

- [ ] **Snapshots present.** `runs/<run-id>/snapshots/` carries payload
      snapshots and the `latest.json` pointer that `resume_reconcile`
      restores from:

  ```powershell
  az storage blob list --container-name harness-state --account-name <storage-account> --auth-mode login --prefix "runs/<run-id>/snapshots/" --output table
  az storage blob show --container-name harness-state --account-name <storage-account> --auth-mode login --name "runs/<run-id>/latest.json"
  ```

- [ ] **Universe cleanliness declared.** The trader universe is the explicit
      `symbols` list in `config/harness/trader.yaml` (equities) and
      `config/harness/trader-crypto.yaml` (crypto). Declare it clean per the
      H9.4 point-in-time universe semantics (`.github/architecture/repository.md`):
      for each symbol and the supervised session date, record that the
      listing window covers the session date, no delisting or rename is
      pending, and no survivorship substitution exists — as-of resolution,
      never a current-membership snapshot. Record the symbols, the as-of
      date, and the coverage evidence pointer in the drill record.

- [ ] **Harness smoke green.** The quant smoke validates the digest-pinned
      image chain, the kill-switch storage, and workload health without
      introducing any Azure client secret (`.github/runbooks/h5-quant-smoke.md`):

  ```powershell
  gh workflow run quant-smoke.yml --repo Carson-Fricke/FCA --ref master
  gh run watch <run-id> --repo Carson-Fricke/FCA
  ```

- [ ] **Alert drills complete** (each alert's drill step in the alerts
      section, including the action group test fire).

### Kill-Switch Drill

Engage the switch, prove a boot halts with exit `41` before any feed is
composed, then disengage. The process prints nothing on the exit path; the
supervision document is the verification surface. A `kill_switch` outcome
resets `consecutive_faults` on the ledger, so the drill is budget-neutral.

```powershell
az containerapp update --name <rth-trader-app> --resource-group <resource-group> --min-replicas 1
az storage entity insert --table-name killswitch --account-name <storage-account> --auth-mode login --if-exists replace --entity PartitionKey=killswitch RowKey=global engaged=true reason="go-live drill"
# expect the next boot to record phase "halted", detail "kill switch engaged at boot", exit 41
az containerapp logs show --name <rth-trader-app> --resource-group <resource-group> --tail 200
az storage blob download --container-name harness-state --account-name <storage-account> --auth-mode login --name "runs/<run-id>/supervision.json" --file supervision.json
az storage entity insert --table-name killswitch --account-name <storage-account> --auth-mode login --if-exists replace --entity PartitionKey=killswitch RowKey=global engaged=false
az containerapp update --name <rth-trader-app> --resource-group <resource-group> --min-replicas 0
```

Mid-session drill (optional, crypto trader): engage while running; the
in-process switch trips at the next poll (`HARNESS_TRADER_KILL_SWITCH_POLL_SECONDS`,
default 10s), a stop snapshot is saved, and the session exits `41` with
`kill-switch engaged:` in the detail.

## Stepped Size Ramp

The ladder bounds every session below the configured risk ceiling. Steps
advance only on evidence; any halt criterion drops the ramp one step. A step
is a configuration change (symbols, quantity, risk rules) deployed through
`deploy-main.yml` — the container image embeds `config/harness/` — plus
optional supervision knobs through container environment variables. Never
skip steps.

Reference bounds (`config/harness/trader.yaml`): `max-order-notional`
`limit: 10000.00` scales down orders above the bound and rejects when no
quantity remains; `capital_fraction: 1.0` allocates the book's
`HARNESS_TRADER_INITIAL_CASH` (default 100000.00) to the single strategy.

| Step | Window | Symbols | Quantity per order | Headroom vs `max-order-notional` | Promotion evidence (all required) |
| --- | --- | --- | --- | --- | --- |
| 0 | Day 1 | SPY (1) | 1 | ≈ 6% of bound | `supervision.json`: `phase` `at_close`/`completed`, `session_status` `at_close`/`completed`, `market_event_count` > 0, `dispatched_count` > 0 |
| 1 | Day 2+ | SPY | 5 (config default) | ≈ 30% of bound | Step 0 evidence for 3 consecutive clean sessions, plus `restarts.json` `consecutive_faults` = 0 across them |
| 2 | Day 5+ | SPY + 2 equities | 5 | ≈ 30% per order | Step 1 evidence for 3 consecutive clean sessions; reconciler clean (see below) |
| 3 | Steady state | Full ramp universe | Size near the bound (e.g. 12) | Approaches the 10000.00 bound | Step 2 evidence plus explicit risk sign-off recorded; add `max-position-notional` and `daily_loss_halt` rules to the trader config at this step |

"Clean session" means every field above plus a clean reconciler pass: the
`recovery` block absent or `action` != `halt`, no detail starting
`reconciliation aborted`, and `boot policy resume_reconcile halted` absent
from the supervision history. The crypto trader ramps on its own ladder
(`config/harness/trader-crypto.yaml`, BTC/USD at 0.01) over UTC-day sessions
with the same evidence fields. Widen `HARNESS_TRADER_MARKET_WAIT_MINUTES`
generously on day 1 (30) and record the reason, per
`.github/runbooks/trader-operations.md`.

## Halt Criteria

Halting means: engage the kill switch (table row), verify the halt through
the supervision document (`phase` `halted`, exit `41`), then investigate.
Every criterion is measurable from a named surface.

| Criterion | Field and threshold | Source | Operator action |
| --- | --- | --- | --- |
| Restartable-fault rate | Any `fault` outcome in `history` within one hour, or `consecutive_faults` ≥ 1 during the ramp | `runs/<run-id>/restarts.json` | Halt; fix the root fault before resuming |
| Crash-loop budget | `consecutive_faults` ≥ `HARNESS_TRADER_RESTART_BUDGET` (default 3) → exit `86` refuses boot | `restarts.json`; exit code | The trader halts itself; fix the root fault, clear the ledger (restart-loop section), verify a clean boot |
| Reconciler abort | Detail beginning `reconciliation aborted:` (periodic, exit `1`) or `recovery.action` = `halt` with `boot policy resume_reconcile halted` (boot, exit `42`) | `supervision.json` | Halt; capture the diff id in `detail` / `recovery.failure`; roll back if the image changed (rollback section) |
| Kill-switch trip | `engaged` = true with a reason you did not write | killswitch table row | Halt is already effective (exit `41`); investigate who engaged and why before disengaging |
| Daily-loss bound | `risk.daily_losses` per strategy instance ≥ the configured `daily_loss_halt` limit; equity drop: latest `books[].equity` below the session's opening snapshot equity minus the limit | `runs/<run-id>/snapshots/latest.json` | The rule halts the strategy at trade time when configured (daily-loss section); otherwise halt manually and treat as a halt criterion |
| Market-wait exceeded | Detail beginning `market-wait exceeded` (exit `75`) | `supervision.json`; exit code | One retry with the platform restart is acceptable; a second within an hour is a halt — check feed, session window, and universe before widening `HARNESS_TRADER_MARKET_WAIT_MINUTES` |

## Rollback

Ordered procedure. The image chain and the GHCR/ACR digest rollback window
are documented in `.github/runbooks/e1-ghcr-retention.md`.

1. **Engage the kill switch** (table row above) and verify the halt (exit
   `41`, `phase` `halted`).
2. **Scale the trader to zero** so no further boots run while the image
   changes:

   ```powershell
   az containerapp update --name <rth-trader-app> --resource-group <resource-group> --min-replicas 0
   ```

3. **Re-import the previous digest.** Preferred: rerun `deploy-main.yml` on
   the previous approved commit, which imports
   `ghcr.io/carson-fricke/fca@<digest>` into the private ACR again and
   redeploys the composition. Fast path: pin the previous mirrored ACR
   digest directly (the ACR prune always keeps the newest two digests and
   every revision-referenced digest):

   ```powershell
   az acr manifest list-metadata --registry <acr-name> --name <repository> --orderby time-desc --output table
   az containerapp update --name <rth-trader-app> --resource-group <resource-group> --image "<acr-login-server>/<repository>@sha256:<previous-digest>"
   ```

4. **Restore scale** (`--min-replicas 0` for the scheduled RTH trader;
   1 for the crypto trader) and watch the next boot.
5. **Verify `resume_reconcile`** from the last snapshot: the supervision
   document walks `boot` → `gate` → `restore` → `connect` → `running`, and
   the `recovery` block records `policy` `resume_reconcile`, `action`
   `proceed`, `report_id`, and the restored `snapshot_id`. A `halt` action
   means the recovery taxonomy refused the resume — resolve the reported
   divergence before continuing.
6. **Disengage the kill switch only after clean supervision documents**
   (preflight's document check, including `consecutive_faults` = 0). Record
   the rollback digest, timestamps, and the reconciled snapshot id in the
   drill record.

## Alerts

| Alert (dev name) | Condition | Channel | Fires today? | Drill step |
| --- | --- | --- | --- | --- |
| `fca-ml-dev-trader-faults` (Log query, severity 2) | `ContainerAppConsoleLogs_CL` rows on trader apps (`ContainerAppName_s` startswith `fca-ml-dev-` and endswith `-trader`) whose `Log_s` matches `restartable fault`, `market-wait exceeded`, `trader feed boundary fault`, or `Traceback` | `fca-ml-dev-trader-alerts` email | Partially: feed-boundary faults and process tracebacks print today; the kernel strings are armed for the console emit path (see below) | Test fire + benign traceback drill below |
| `fca-ml-dev-trader-halts` (Log query, severity 1) | Same scope; `Log_s` matches `kill-switch engaged`, `reconciliation aborted`, `daily_loss_halt limit reached`, `restart budget exhausted`, or `supervision ledger write failed` | `fca-ml-dev-trader-alerts` email | Armed only: those details live in `supervision.json` today; no console emit path exists yet — the manual checks below cover them | Test fire + manual KQL verification below |
| `fca-ml-dev-trader-restarts` (Activity log) | Administrative `Microsoft.App/containerApps/write` or `Microsoft.App/containerApps/revisions/restart/action` with `trader` in the resource URI | `fca-ml-dev-trader-alerts` email | Yes | Restart-revision drill below |
| Kill-switch engaged event | `engaged` = true on `(PartitionKey='killswitch', RowKey='global')` | Manual check | Manual: no table diagnostics are wired | Part of the kill-switch drill: query the row |
| Daily-loss bound | `risk.daily_losses` in the latest snapshot vs the configured limit | Manual daily check | Manual: no automated surface publishes trader risk decisions today | Part of the daily check |

**Test fire (every alert's delivery proof).** The action group sends one test
notification per alert type; repeat for `logalertv2` and `activitylog`:

```powershell
az monitor action-group test-notifications create --action-group <trader-alerts-action-group> --resource-group <resource-group> --alert-type logalertv2 --add-action email operator <operator-email>
az monitor action-group test-notifications create --action-group <trader-alerts-action-group> --resource-group <resource-group> --alert-type activitylog --add-action email operator <operator-email>
```

**Benign traceback drill (fault-class query alert).** A non-numeric
supervision knob makes the process boundary raise before any session,
credential use, or market interaction — verified to print a Python traceback
to console logs and exit 1:

```powershell
az containerapp update --name <crypto-trader-app> --resource-group <resource-group> --set-env-vars HARNESS_TRADER_MARKET_WAIT_MINUTES=drill
# expect a traceback in the console logs and the faults alert within its window
az containerapp logs show --name <crypto-trader-app> --resource-group <resource-group> --tail 100
az containerapp update --name <crypto-trader-app> --resource-group <resource-group> --remove-env-vars HARNESS_TRADER_MARKET_WAIT_MINUTES
```

**Manual KQL verification (both query alerts).** Run each rule's exact query
against the workspace over the drill window and confirm it resolves and, for
the traceback drill, returns the matching rows:

```powershell
$workspace = az monitor log-analytics workspace list --resource-group <resource-group> --query "[0].customerId" --output tsv
az monitor log-analytics query --workspace $workspace --timespan PT1H --analytics-query "ContainerAppConsoleLogs_CL | where ContainerAppName_s startswith 'fca-ml-dev-' and ContainerAppName_s endswith '-trader' | where Log_s has_any ('restartable fault', 'market-wait exceeded', 'trader feed boundary fault', 'Traceback') | project TimeGenerated, ContainerAppName_s, Log_s"
```

**Restart-revision drill (activity-log alert).** Restart the crypto trader
revision; the operation fires the activity-log alert within its evaluation
window, and the drill record captures the notification:

```powershell
az containerapp revision restart --name <crypto-trader-app> --resource-group <resource-group> --revision <revision-name>
```

**Automatable-today boundary.** The supervision kernel writes its decisions
to the `supervision.json` document, not to console logs: the faulted-phase,
consecutive-fault, kill-switch, and reconciler details are manual checks
today. Wiring a console emit (the supervisor's `emit` boundary) lights the
armed halt-class rule without a query change; until then, run the manual
daily check below and keep the drill record.

**Manual daily check.** Fetch `runs/<run-id>/supervision.json` and
`runs/<run-id>/snapshots/latest.json` from the `harness-state` container and
record: `phase`, `consecutive_faults`, the killswitch row state, and
`risk.daily_losses` per strategy instance against the configured
`daily_loss_halt` limit; compare `books[].equity` against the run's opening
snapshot equity. Any threshold breach is a halt criterion.

## Restart Loop, Watchdog, Reconciler, Kill, and Daily Loss

**Restart loop.** Cross-process restarts are bounded by the durable ledger:
`restarts.json` counts consecutive `fault` outcomes (clean and `kill_switch`
outcomes reset the counter, history keeps the last eight). At
`HARNESS_TRADER_RESTART_BUDGET` (default 3) the next boot exits `86`
(`crash_loop`) before any gate. Remediation ladder: read `history` and the
fault details → fix the root fault (`Common Failures` in
`.github/runbooks/trader-operations.md`) → delete
`runs/<run-id>/restarts.json` → verify the next boot is clean and
`consecutive_faults` = 0. A spent budget never self-clears.

**Watchdog.** Two supervision watchdogs fail closed with exit `75`:
`market-wait exceeded` when no market events arrive by boot/open time plus
`HARNESS_TRADER_MARKET_WAIT_MINUTES` (default 15), and the kill-switch stale
poll after `HARNESS_TRADER_KILL_SWITCH_STALE` (default 3) consecutive poll
failures — a poll error is never a silent proceed. Remediation ladder:
market-wait → session window, feed connectivity, subscribed universe; widen
the window only with a recorded reason. Stale polls → table existence,
`HARNESS_KILL_SWITCH_TABLE` match, and the runtime identity's table data
access; the platform restart re-gates the boot.

**Reconciler.** `resume_reconcile` reconciles the restored books against the
snapshot-derived broker state at boot; a divergence halts through the
recovery taxonomy with exit `42` (`recovery.action` = `halt`,
`recovery.failure` carrying the report). The periodic reconciler runs every
`HARNESS_TRADER_RECONCILE_MINUTES` (default 15) and an abort stops the
session with detail `reconciliation aborted: <diff-id>` and exit `1` — a
supervision defect, not a transient fault. Remediation ladder: capture the
diff id and recovery report → halt via the switch → roll back the image if
it changed → resolve the divergence before the next boot.

**Kill.** The global switch is one Azure Table row
(`(PartitionKey='killswitch', RowKey='global')`, boolean `engaged`, optional
`reason`; a missing row is disengaged). A boot with the switch engaged exits
`41` before any feed is composed; a running session trips its in-process
switch at the next poll, saves a stop snapshot, and exits `41`. The table row
is the audit record for halts — keep `reason` human-readable and free of
secret values.

**Daily loss.** `harness.risk.limits:DailyLossHalt` rejects and marks a
halt when the strategy's exact daily loss reaches its limit (`daily_loss_halt
limit reached`, `on_reject: halt_strategy`); the value is captured per
strategy instance in every snapshot (`risk.daily_losses`) and drives the
manual daily check and the halt criterion above. The reference trader config
ships `max-order-notional` only — add `daily_loss_halt` and
`max-position-notional` at ramp step 3 through the normal deploy path.

## Drill Record

Record per drill: date, operator, the commands executed (verbatim), the
observed supervision fields (`phase`, `detail`, `consecutive_faults`,
`recovery.action`), the alert notifications received with timestamps, and
for rollbacks the from/to image digests and the reconciled `snapshot_id`.
Never record secret values, credential material, or provider responses.
