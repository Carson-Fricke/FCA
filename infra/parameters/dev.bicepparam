using '../main.bicep'

// Replace both sentinels before any Azure deployment or what-if.
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
