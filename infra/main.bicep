targetScope = 'resourceGroup'

@description('Azure region for the complete market-lake resource-group deployment.')
param location string = resourceGroup().location

@description('Short project prefix used to derive deterministic resource names.')
@minLength(2)
@maxLength(16)
param resourceNamePrefix string = 'fca-ml'

@description('Deployment environment suffix used in deterministic resource names.')
@minLength(1)
@maxLength(12)
param environmentName string = 'dev'

@description('Immutable, anonymously pullable GHCR image reference. Replace the dev sentinel with the digest emitted by the image publication workflow before deployment.')
@minLength(1)
param sharedImageReference string

@description('GitHub organization or user owning the repository. This public value must match the immutable OIDC subject casing.')
@minLength(1)
param githubOwner string

@description('Public numeric GitHub database ID of the repository owner.')
@minLength(1)
param githubOwnerId string

@description('GitHub repository name without the owner prefix. This public value must match the immutable OIDC subject casing.')
@minLength(1)
param githubRepository string

@description('Public numeric GitHub database ID of the repository.')
@minLength(1)
param githubRepositoryId string

@description('GitHub Environment trusted by the deployer federated credential.')
@minLength(1)
param githubEnvironment string

@description('First day of the monthly budget period in UTC ISO 8601 format. Update this value to the first day of the intended deployment month.')
@minLength(10)
param budgetStartDate string

@description('REQUIRED USER INPUT: valid email address for Azure Cost Management budget alerts. The dev parameter file contains a sentinel placeholder that Azure must not receive.')
@minLength(1)
param budgetAlertContactEmail string

@description('Whether to enable the one-time GitHub deployer bootstrap role assignments. Keep false for recurring deployments after bootstrap.')
param assignGithubDeployerRoles bool = false

@description('Whether the Key Vault public endpoint is enabled. The dev value is explicit because this composition contains no private endpoint or private DNS modules; RBAC authorization remains required.')
param allowPublicNetworkAccess bool = false

@description('Whether the API accepts public ingress. Keep disabled until the API authentication boundary is ready.')
param enableExternalApiIngress bool = false

@description('Five-field UTC cron expression for the scheduled ingest job.')
param ingestCronExpression string = '0 1 * * *'

@description('Maximum seconds allowed for one ingest job replica execution.')
param ingestReplicaTimeoutSeconds int = 3600

var normalizedPrefix = toLower(resourceNamePrefix)
var normalizedEnvironment = toLower(environmentName)
var namePrefix = '${normalizedPrefix}-${normalizedEnvironment}'
var shortNamePrefix = '${take(normalizedPrefix, 8)}-${take(normalizedEnvironment, 4)}'
var deterministicSuffix = uniqueString(resourceGroup().id, namePrefix)
var storageAccountName = 'fca${take(deterministicSuffix, 18)}'
var keyVaultName = '${shortNamePrefix}-kv-${take(deterministicSuffix, 7)}'
var runtimeIdentityName = '${namePrefix}-runtime'
var githubDeployerIdentityName = '${namePrefix}-github-deployer'
var logAnalyticsWorkspaceName = '${namePrefix}-logs'
var containerAppsEnvironmentName = '${namePrefix}-cae'
var apiAppName = '${namePrefix}-api'
var ingestJobName = '${namePrefix}-ingest'
var budgetName = '${namePrefix}-budget'
var sharedLogRetentionDays = 30
var tags = {
  application: 'fca'
  product: 'market-lake'
  environment: environmentName
  managedBy: 'bicep'
}

module storage './modules/storage.bicep' = {
  name: 'market-lake-storage'
  params: {
    storageAccountName: storageAccountName
    location: location
    tags: tags
    coolAfterDays: 14
    enableColdTier: false
  }
}

module identities './modules/identities.bicep' = {
  name: 'market-lake-identities'
  params: {
    runtimeIdentityName: runtimeIdentityName
    githubDeployerIdentityName: githubDeployerIdentityName
    location: location
    tags: tags
    githubOwner: githubOwner
    githubOwnerId: githubOwnerId
    githubRepository: githubRepository
    githubRepositoryId: githubRepositoryId
    githubEnvironment: githubEnvironment
  }
}

module keyVault './modules/key-vault.bicep' = {
  name: 'market-lake-key-vault'
  params: {
    keyVaultName: keyVaultName
    location: location
    tags: tags
    allowPublicNetworkAccess: allowPublicNetworkAccess
  }
}

module access './modules/access.bicep' = {
  name: 'market-lake-access'
  params: {
    runtimePrincipalId: identities.outputs.runtimeIdentity.principalId
    githubDeployerPrincipalId: identities.outputs.githubDeployerIdentity.principalId
    storageAccountResourceId: storage.outputs.storageAccountId
    keyVaultResourceId: keyVault.outputs.keyVaultId
    assignGithubDeployerRoles: assignGithubDeployerRoles
  }
}

module containerApps './modules/container-apps.bicep' = {
  name: 'market-lake-container-apps'
  params: {
    logAnalyticsWorkspaceName: logAnalyticsWorkspaceName
    containerAppsEnvironmentName: containerAppsEnvironmentName
    apiAppName: apiAppName
    ingestJobName: ingestJobName
    sharedImageReference: sharedImageReference
    runtimeIdentityResourceId: identities.outputs.runtimeIdentity.resourceId
    runtimeIdentityClientId: identities.outputs.runtimeIdentity.clientId
    location: location
    tags: tags
    ingestCronExpression: ingestCronExpression
    ingestReplicaTimeoutSeconds: ingestReplicaTimeoutSeconds
    // Cost controls declares this shared workspace with public query disabled.
    allowPublicLogQuery: false
    enableExternalApiIngress: enableExternalApiIngress
  }
}

module costControls './modules/cost-controls.bicep' = {
  name: 'market-lake-cost-controls'
  dependsOn: [
    containerApps
  ]
  params: {
    logAnalyticsWorkspaceName: logAnalyticsWorkspaceName
    budgetName: budgetName
    location: location
    tags: tags
    // The Container Apps module also declares this shared workspace at 30 days.
    logRetentionDays: sharedLogRetentionDays
    firstBudgetAlertUsd: 40
    secondBudgetAlertUsd: 70
    budgetStartDate: budgetStartDate
    alertContactEmails: [
      budgetAlertContactEmail
    ]
    storageCapacityBytes: 1099511627776
    pauseEnqueueAtCapacity: true
    pauseIngestAtCapacity: true
    backfillMaxReplicas: 1
    sqlMaxScanBytes: 1073741824
    sqlMaxResultBytes: 33554432
  }
}

@description('Non-secret summary of the composed market-lake resources and identity wiring.')
output deploymentSummary object = {
  environment: environmentName
  location: location
  names: {
    storageAccount: storage.outputs.storageAccountName
    keyVault: keyVault.outputs.keyVaultName
    runtimeIdentity: identities.outputs.runtimeIdentity.name
    githubDeployerIdentity: identities.outputs.githubDeployerIdentity.name
    logAnalyticsWorkspace: logAnalyticsWorkspaceName
    containerAppsEnvironment: containerAppsEnvironmentName
    api: apiAppName
    ingestJob: ingestJobName
  }
  image: {
    reference: sharedImageReference
    pinnedByDigest: contains(sharedImageReference, '@sha256:')
  }
  runtimeIdentity: identities.outputs.runtimeIdentity
  roleAssignments: access.outputs.runtimeRoleAssignments
  keyVaultNetwork: keyVault.outputs.networkAccess
  containerApps: containerApps.outputs
  costControls: costControls.outputs
}
