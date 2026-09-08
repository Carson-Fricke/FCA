# H5 Quant Runtime Smoke

The deployment pipeline deploys the quant-harness runtime through the same
OIDC path as the market lake and then smoke-validates it. This runbook
documents what the smoke checks, which failures are expected before runtime
secrets are seeded, and how to trigger the smoke manually. It extends
`.github/runbooks/github-azure-oidc-bootstrap.md`; read that runbook for the
bootstrap, trust, and permissions model. Never echo secret values in logs,
issues, or task evidence.

## Deployment And Image Chain

`deploy-main.yml` on `master` runs three stages:

1. `publish-image` builds one immutable shared image and returns its GHCR
   digest.
2. `deploy` imports that exact digest into the private ACR, deploys
   `infra/main.bicep`, and verifies that the lake API, ingest job, quant API,
   RTH trader, optional crypto trader, and backtest job all reference the same
   mirrored ACR digest. The quant runtime shares the lake image; there is no
   separate quant image to publish. The quant API runs `harness api`, traders
   run `harness trader`, and the backtest job runs `backtest-job` from that
   one image.
3. `quant-smoke` calls `.github/workflows/quant-smoke.yml` with the published
   digest and validates the deployed quant runtime without introducing any
   Azure client secret.

The smoke job authenticates with the same environment-scoped OIDC federated
credential as the deploy job (`id-token: write`, no secrets) and targets the
environment selected by the `DEPLOYMENT_ENVIRONMENT` repository variable.

## What The Smoke Validates

| Check | Passes when | Fails the smoke |
| --- | --- | --- |
| Image digest pinning | The quant API, RTH trader, optional crypto trader, and backtest job reference digest-pinned private ACR images; with an expected digest input, exactly that digest | Yes |
| Ingress posture | Quant API ingress `external` matches `ENABLE_EXTERNAL_API_INGRESS`, `allowInsecure` is `false`, and the FQDN exists | Yes |
| Kill-switch storage | The `killswitch` table and the `harness-results` and `harness-state` blob containers exist on the tagged runtime storage account | Yes |
| Quant API health | Public ingress: `/health` returns `status: ok` and `/v1/auth/check` rejects anonymous access with 401. Private ingress: the app is scaled to one replica, passes all three `/health` probes, and is restored to zero minimum replicas through an exit trap | Yes |
| Backtest job | `az containerapp job start` launches the in-image bounded synthetic configuration `config/harness/backtest.yaml` and the execution reaches `Succeeded` before the deadline | Yes |
| Trader revision health | RTH trader and optional crypto trader revisions are reported as `healthy`, `dormant` (zero minimum replicas), or `degraded` | No |

The backtest smoke is bounded and non-destructive: `config/harness/backtest.yaml`
runs the deterministic synthetic benchmark fixture inside the container with
no data directory, no network access, and no credentials, writing artifacts
only to the container filesystem. The smoke polls that one execution with a
deadline (`BACKTEST_SMOKE_DEADLINE_SECONDS`, default 600) and never reads its
logs for secret material.

The kill-switch and harness container checks use the ARM management plane
(`az rest`) because the GitHub deployer identity intentionally holds no
storage data-plane roles and the storage account disables shared keys. The
runtime identity `fca-ml-<environment>-quant-runtime` is the principal that
reads and writes these resources at the data plane.

## Expected Non-Failing Statuses

A green pipeline does not require green traders. The smoke reports trader
revision health as a distinct status instead of failing:

- `dormant`: the RTH trader is provisioned at zero minimum replicas. It runs
  only inside its KEDA cron session window
  (`rthScheduleStart` through `rthScheduleEnd` in
  `RTH_SCHEDULE_TIMEZONE`), so the smoke observes it without scaling it up.
- `degraded`: the revision is not healthy. This is expected until both of the
  following are true:
  1. The environment vault holds the shared Alpaca credential pair
      (`alpaca-api-key` and `alpaca-api-secret`; one key pair serves both
      paper and live modes, per
      `.github/runbooks/key-vault-runtime-secrets.md`). Unresolved Key Vault
      references stop replica startup, which appears as an unhealthy revision
      for the always-on crypto trader.
  2. The harness trader runtime is deployed. `harness trader` runs one
      supervised session (`harness.cli.supervisor`) and exits outside its
      KEDA session window; a stale revision from before the trader runtime
      landed appears unhealthy.

Degraded traders produce a `::warning` annotation and a summary note; they do
not fail the workflow. Fail the pipeline on trader status only after the
trader runtime implementation task defines its own acceptance gates.

## Green Smoke Prerequisites

The smoke itself needs no secrets: the healthy quant API revision already
proves that the versionless Key Vault reference for `harness-api-bearer-token`
resolves, and the backtest job consumes no secrets. A fully green trader
status additionally requires the environment vault to contain, per
`RTH_TRADER_CREDENTIAL_MODE` and `CRYPTO_TRADER_CREDENTIAL_MODE`:

| Key Vault secret | Consumer | Used by the smoke |
| --- | --- | --- |
| `harness-api-bearer-token` | Quant API | Proven by revision startup, not read by CI |
| `alpaca-api-key` / `alpaca-api-secret` | Traders in both paper and live modes | Proven only when a trader replica starts |

Seed values with the masked-prompt helpers in
`.github/runbooks/key-vault-runtime-secrets.md`; the quant names live in the
same environment vault. The GitHub deployer identity must never receive
`Key Vault Secrets User`, and the smoke must never fetch or print the bearer
token. Protected-route verification with the bearer value remains a manual,
human-run check from an approved network path, as described in the OIDC
runbook.

## Manual Triggers

Run the smoke without deploying from the Actions tab or `gh workflow run`:

```powershell
gh workflow run quant-smoke.yml --repo Carson-Fricke/FCA --ref master
gh run watch <run-id> --repo Carson-Fricke/FCA
```

- The optional `image-digest` input pins verification to one exact digest;
  leave it empty to verify that the quant workloads share one digest-pinned
  image.
- The job targets the `DEPLOYMENT_ENVIRONMENT` environment and its branch
  policy, so manual triggers must run from `master` while the dev
  `master`-only deployment branch policy is active.
- The smoke is safe to rerun: it is read-only except for the temporary
  one-replica quant API probe (restored through an exit trap) and the bounded
  synthetic backtest execution.

## Recovery

- Digest mismatch: the deployed revision predates the latest image; rerun
  `deploy-main.yml` for the approved commit before investigating the smoke.
- Missing table or container: the storage composition changed or was deleted
  outside CI; redeploy `infra/main.bicep` and rerun the smoke.
- Backtest execution `Failed`: inspect that execution's logs in Azure for the
  container exit; the synthetic configuration requires no credentials, so a
  failure indicates image, configuration, or platform problems rather than
  unseeded secrets.
- Quant API probes failing after seed or rotation: confirm enabled current
  versions, the `harness-api-bearer-token` reference, and the quant runtime
  identity's `Key Vault Secrets User` role, then allow for reference-refresh
  delay per the Key Vault runbook.
- Trader `degraded` after seeding: verify secret names and credential-mode
  selection first, then inspect the revision events; a healthy deployment
  with an exiting replica points at supervision settings rather than
  secrets. See `.github/runbooks/trader-operations.md` for the exit-code
  taxonomy and remediation.
