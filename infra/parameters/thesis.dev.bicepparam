using '../main.bicep'

// Thesis-enabled dev composition. Everything below the market-lake block is
// additive: the same resource group, prefix, and identities, with the Thesis
// backend and its workspace persistence enabled. Replace both sentinels
// before any Azure deployment or what-if.
param location = 'westus2'
param resourceNamePrefix = 'fca-ml'
param environmentName = 'dev'
param sharedImageReference = 'example.azurecr.io/fca@sha256:0000000000000000000000000000000000000000000000000000000000000000'
param githubOwner = 'Carson-Fricke'
param githubOwnerId = '48462838'
param githubRepository = 'FCA'
param githubRepositoryId = '1347935926'
param githubEnvironment = 'dev'
param budgetStartDate = '2026-09-01'
param budgetAlertContactEmail = 'REPLACE_WITH_BUDGET_CONTACT_EMAIL'
param assignGithubDeployerRoles = false
// Public access is explicit for dev because no private endpoint module is composed here.
param allowPublicNetworkAccess = true
param enableExternalApiIngress = false
param ingestCronExpression = '0 1 * * *'
param ingestReplicaTimeoutSeconds = 3600
param ingestMaxRecords = 1000
// Crypto compute stays absent in dev until the always-on trader is enabled explicitly.
param enableCryptoTrader = false
// Dev traders consume the paper credential pair only; live is enabled per environment.
param rthTraderMode = 'paper'
param cryptoTraderMode = 'paper'
// RTH schedule: XNYS regular trading hours, Monday through Friday, New York time.
param rthScheduleTimezone = 'America/New_York'
param rthScheduleStart = '30 9 * * 1-5'
param rthScheduleEnd = '0 16 * * 1-5'
param rthTraderReplicas = 1
param backtestReplicaTimeoutSeconds = 3600
// fca-ml-lake-api application client ID from the Entra bootstrap runbook.
param lakeApiAudience = 'cbc572d2-ae2e-4501-af76-0515e2f73bcd'

// Thesis is explicitly enabled in this parameter file; the market-lake
// default stays false so existing deployments are unchanged.
param deployThesis = true
// Digest-pinned Thesis image reference. Replace with the ACR login server
// and the published sha256 digest from the deploy-thesis workflow; the
// zero-digest sentinel is only valid for what-if and validation.
param thesisImageReference = 'example.azurecr.io/fca-thesis@sha256:0000000000000000000000000000000000000000000000000000000000000000'
// The Thesis API stays on internal ingress until its authentication boundary is ready.
param thesisEnableExternalIngress = false
// Empty keeps the Thesis API on the static bearer token; set an Entra app client ID to enable Entra validation.
param thesisApiAudience = ''
// Scale-to-zero with a bounded two-replica ceiling keeps idle compute at $0.
param thesisContainerCpu = '0.25'
param thesisContainerMemory = '0.5Gi'
param thesisMinReplicas = 0
param thesisMaxReplicas = 2
// The public SvelteKit SPA host is enabled in dev. The linked backend and
// custom-domain binding are added by a later task (T301).
param deployThesisStaticWebApp = true
// Empty derives a deterministic globally unique Static Web App name from the
// resource group, so no operator input is required.
param thesisStaticWebAppName = ''
// Empty keeps the generated azurestaticapps.net hostname until T301 binds an
// operator-owned custom domain.
param thesisStaticWebAppCustomDomain = ''
// Seed registry matching config/thesis/workspace.yaml; the runbook seeds the
// matching workspaces table row after deployment.
param thesisWorkspaceRegistry = [
  {
    workspaceId: 'equity-momentum'
    ownerId: 'local-operator'
  }
]
