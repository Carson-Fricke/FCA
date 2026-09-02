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
param ingestCronExpression = '0 1 * * *'
param ingestReplicaTimeoutSeconds = 3600
param ingestMaxRecords = 1000
