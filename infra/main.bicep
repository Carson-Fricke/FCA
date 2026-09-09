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

@description('Immutable private ACR image reference imported from the GHCR publication digest before deployment.')
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

@description('Entra application client ID the API accepts as token audience. Empty keeps the API on static bearer authentication only.')
param lakeApiAudience string = ''

@description('Maximum seconds allowed for one ingest job replica execution.')
param ingestReplicaTimeoutSeconds int = 3600

@description('Maximum Alpaca records fetched per data type for one bounded ingest execution.')
@minValue(1)
@maxValue(100000)
param ingestMaxRecords int = 1000

@description('Whether to deploy the always-on crypto trader Container App. The scheduled RTH equities trader is always deployed; false leaves no crypto compute in the resource group.')
param enableCryptoTrader bool = false

@allowed([
  'paper'
  'live'
])
@description('Which Key Vault Alpaca credential pair the scheduled RTH trader consumes. Secret values stay in Key Vault; only the referenced secret names change.')
param rthTraderMode string = 'paper'

@allowed([
  'paper'
  'live'
])
@description('Which Key Vault Alpaca credential pair the crypto trader consumes.')
param cryptoTraderMode string = 'paper'

@description('IANA timezone applied to the RTH schedule scale rule.')
@minLength(1)
param rthScheduleTimezone string = 'America/New_York'

@description('Five-field cron expression that opens the RTH scale window at the equity session start.')
@minLength(9)
param rthScheduleStart string = '30 9 * * 1-5'

@description('Five-field cron expression that closes the RTH scale window at the equity session end.')
@minLength(9)
param rthScheduleEnd string = '0 16 * * 1-5'

@description('Replica count the RTH schedule rule applies while the session window is open.')
@minValue(1)
@maxValue(4)
param rthTraderReplicas int = 1

@description('Maximum seconds allowed for one backtest job replica execution.')
@minValue(60)
@maxValue(86400)
param backtestReplicaTimeoutSeconds int = 3600

// Thesis (T174) wiring is additive: deployThesis defaults to false so the
// market-lake composition is unchanged. The Thesis identity is composed here
// rather than in identities.bicep because that module is shared and the
// Thesis runtime requires an identity distinct from both the lake runtime
// identity and the quant-harness identity; Thesis must never share the
// provider, broker, or gatekeeper credential scope of those workloads
// (fca-thesis.md security invariants). The thesis modules receive the
// identity through parameters and create no identity themselves.
@description('Whether to deploy the Thesis backend Container App and its workspace persistence. False leaves the market-lake composition unchanged.')
param deployThesis bool = false

@description('Immutable digest-pinned private ACR image reference for the Thesis backend image built from Dockerfile.thesis. Required when deployThesis is true.')
@minLength(1)
param thesisImageReference string = 'not-deployed.azurecr.io/thesis@sha256:0000000000000000000000000000000000000000000000000000000000000000'

@description('Whether the Thesis API accepts public ingress, independent of the market-lake API authentication boundary.')
param thesisEnableExternalIngress bool = false

@description('Entra application client ID the Thesis API accepts as token audience. Empty keeps the Thesis API on static bearer authentication only.')
param thesisApiAudience string = ''

@description('Operator-owned hostname served by the Thesis API. Empty keeps the Container Apps default FQDN; a non-empty value adds a hostname binding and a managed certificate gated on registrar-side DNS validation (domain-hosting.md).')
param thesisCustomDomain string = ''

@description('Initial Thesis workspace registry. Each entry requires workspaceId and ownerId; one blob container is provisioned per entry.')
param thesisWorkspaceRegistry array = []

@description('CPU cores per Thesis API replica.')
param thesisContainerCpu string = '0.25'

@description('Memory per Thesis API replica.')
param thesisContainerMemory string = '0.5Gi'

@description('Minimum replicas for the Thesis API; zero scales the app to zero.')
@minValue(0)
@maxValue(4)
param thesisMinReplicas int = 0

@description('Maximum replicas for the Thesis API; bounds the additive monthly cost envelope.')
@minValue(1)
@maxValue(4)
param thesisMaxReplicas int = 2

var normalizedPrefix = toLower(resourceNamePrefix)
var normalizedEnvironment = toLower(environmentName)
var namePrefix = '${normalizedPrefix}-${normalizedEnvironment}'
var shortNamePrefix = '${take(normalizedPrefix, 8)}-${take(normalizedEnvironment, 4)}'
var deterministicSuffix = uniqueString(resourceGroup().id, namePrefix)
var storageAccountName = 'fca${take(deterministicSuffix, 18)}'
var keyVaultName = '${shortNamePrefix}-kv-${take(deterministicSuffix, 7)}'
var runtimeIdentityName = '${namePrefix}-runtime'
var quantRuntimeIdentityName = '${namePrefix}-quant-runtime'
var githubDeployerIdentityName = '${namePrefix}-github-deployer'
var containerRegistryName = 'fca${take(uniqueString(resourceGroup().id, namePrefix, 'registry'), 18)}'
var logAnalyticsWorkspaceName = '${namePrefix}-logs'
var containerAppsEnvironmentName = '${namePrefix}-cae'
var apiAppName = '${namePrefix}-api'
var ingestJobName = '${namePrefix}-ingest'
var quantApiAppName = '${namePrefix}-quant-api'
var rthTraderAppName = '${namePrefix}-rth-trader'
var cryptoTraderAppName = '${namePrefix}-crypto-trader'
var backtestJobName = '${namePrefix}-backtest'
var thesisIdentityName = '${namePrefix}-thesis'
var thesisApiAppName = '${namePrefix}-thesis-api'
// A dedicated persistence account for Thesis workspaces: 'thesis' plus the
// same deterministic suffix keeps the name distinct from the lake account.
var thesisWorkspacesStorageAccountName = 'thesis${take(deterministicSuffix, 18)}'
var budgetName = '${namePrefix}-budget'
// The PerGB2018 Log Analytics SKU requires at least 30 days.
var logRetentionDays = 30
var tags = {
  application: 'fca'
  product: 'market-lake'
  environment: environmentName
  managedBy: 'bicep'
}

resource thesisIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = if (deployThesis) {
  name: thesisIdentityName
  location: location
  tags: tags
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
    quantRuntimeIdentityName: quantRuntimeIdentityName
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

module containerRegistry './modules/container-registry.bicep' = {
  name: 'market-lake-container-registry'
  params: {
    registryName: containerRegistryName
    runtimePrincipalId: identities.outputs.runtimeIdentity.principalId
    location: location
    tags: tags
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
    quantPrincipalId: identities.outputs.quantRuntimeIdentity.principalId
    githubDeployerPrincipalId: identities.outputs.githubDeployerIdentity.principalId
    storageAccountResourceId: storage.outputs.storageAccountId
    keyVaultResourceId: keyVault.outputs.keyVaultId
    assignGithubDeployerRoles: assignGithubDeployerRoles
  }
}

module containerApps './modules/container-apps.bicep' = {
  name: 'market-lake-container-apps'
  dependsOn: [
    access
  ]
  params: {
    logAnalyticsWorkspaceName: logAnalyticsWorkspaceName
    containerAppsEnvironmentName: containerAppsEnvironmentName
    apiAppName: apiAppName
    ingestJobName: ingestJobName
    sharedImageReference: sharedImageReference
    registryLoginServer: containerRegistry.outputs.registry.loginServer
    storageAccountName: storage.outputs.storageAccountName
    keyVaultUri: keyVault.outputs.keyVaultUri
    runtimeIdentityResourceId: identities.outputs.runtimeIdentity.resourceId
    runtimeIdentityClientId: identities.outputs.runtimeIdentity.clientId
    location: location
    tags: tags
    logRetentionDays: logRetentionDays
    ingestCronExpression: ingestCronExpression
    ingestReplicaTimeoutSeconds: ingestReplicaTimeoutSeconds
    ingestMaxRecords: ingestMaxRecords
    allowPublicLogQuery: false
    enableExternalApiIngress: enableExternalApiIngress
    lakeApiTenantId: tenant().tenantId
    lakeApiAudience: lakeApiAudience
    quantApiAppName: quantApiAppName
    rthTraderAppName: rthTraderAppName
    cryptoTraderAppName: cryptoTraderAppName
    backtestJobName: backtestJobName
    quantIdentityResourceId: identities.outputs.quantRuntimeIdentity.resourceId
    quantIdentityClientId: identities.outputs.quantRuntimeIdentity.clientId
    enableCryptoTrader: enableCryptoTrader
    rthTraderMode: rthTraderMode
    cryptoTraderMode: cryptoTraderMode
    rthScheduleTimezone: rthScheduleTimezone
    rthScheduleStart: rthScheduleStart
    rthScheduleEnd: rthScheduleEnd
    rthTraderReplicas: rthTraderReplicas
    backtestReplicaTimeoutSeconds: backtestReplicaTimeoutSeconds
  }
}

module costControls './modules/cost-controls.bicep' = {
  name: 'market-lake-cost-controls'
  params: {
    budgetName: budgetName
    logRetentionDays: logRetentionDays
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


module traderMonitoring './modules/monitoring.bicep' = {
  name: 'market-lake-trader-monitoring'
  params: {
    namePrefix: namePrefix
    logAnalyticsWorkspaceName: logAnalyticsWorkspaceName
    traderAlertContactEmail: budgetAlertContactEmail
    tags: tags
  }
}
module thesisWorkspaces './modules/thesis-workspaces.bicep' = if (deployThesis) {
  name: 'thesis-workspaces'
  params: {
    storageAccountName: thesisWorkspacesStorageAccountName
    location: location
    tags: tags
    // Ternary guards mirror the conditional module so the possibly-skipped
    // identity is never dereferenced when Thesis is disabled; the filler
    // values are never evaluated in an enabled deployment.
    thesisPrincipalId: deployThesis ? thesisIdentity.properties.principalId : '00000000-0000-0000-0000-000000000000'
    workspaceRegistry: thesisWorkspaceRegistry
  }
}

module thesisApi './modules/thesis.bicep' = if (deployThesis) {
  name: 'thesis-api'
  // Param expressions referencing containerApps, keyVault, and
  // thesisWorkspaces outputs create the deployment ordering implicitly; an
  // explicit dependsOn would be flagged as unnecessary. The identity and
  // workspace accesses use the same ternary guard as their conditional
  // sources so a Thesis-disabled deployment never dereferences them.
  params: {
    thesisApiAppName: thesisApiAppName
    containerAppsEnvironmentResourceId: containerApps.outputs.containerAppsEnvironment.resourceId
    logAnalyticsWorkspaceResourceId: containerApps.outputs.logAnalyticsWorkspace.resourceId
    thesisImageReference: thesisImageReference
    registryLoginServer: containerRegistry.outputs.registry.loginServer
    keyVaultUri: keyVault.outputs.keyVaultUri
    thesisIdentityResourceId: deployThesis ? thesisIdentity.id : '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/not-deployed'
    thesisIdentityClientId: deployThesis ? thesisIdentity.properties.clientId : '00000000-0000-0000-0000-000000000000'
    thesisIdentityPrincipalId: deployThesis ? thesisIdentity.properties.principalId : '00000000-0000-0000-0000-000000000000'
    thesisWorkspaceStorageAccountName: deployThesis ? thesisWorkspaces.outputs.storageAccountName : 'thesisnotdeployed'
    thesisWorkspaceContainerPrefix: deployThesis ? thesisWorkspaces.outputs.containerPrefix : 'thesis-ws-'
    thesisWorkspaceRegistryTableName: deployThesis ? thesisWorkspaces.outputs.registryTableName : 'workspaces'
    location: location
    tags: tags
    enableExternalIngress: thesisEnableExternalIngress
    thesisApiEntraAudience: thesisApiAudience
    customDomainName: thesisCustomDomain
    thesisContainerCpu: thesisContainerCpu
    thesisContainerMemory: thesisContainerMemory
    thesisMinReplicas: thesisMinReplicas
    thesisMaxReplicas: thesisMaxReplicas
  }
}

@description('Non-secret summary of the composed market-lake and quant-harness resources, identity wiring, and additive cost controls.')
output deploymentSummary object = {
  environment: environmentName
  location: location
  names: {
    storageAccount: storage.outputs.storageAccountName
    keyVault: keyVault.outputs.keyVaultName
    runtimeIdentity: identities.outputs.runtimeIdentity.name
    quantRuntimeIdentity: identities.outputs.quantRuntimeIdentity.name
    githubDeployerIdentity: identities.outputs.githubDeployerIdentity.name
    logAnalyticsWorkspace: logAnalyticsWorkspaceName
    containerAppsEnvironment: containerAppsEnvironmentName
    api: apiAppName
    ingestJob: ingestJobName
    quantApi: quantApiAppName
    rthTrader: rthTraderAppName
    cryptoTrader: cryptoTraderAppName
    backtestJob: backtestJobName
    containerRegistry: containerRegistry.outputs.registry.name
  }
  image: {
    reference: sharedImageReference
    pinnedByDigest: contains(sharedImageReference, '@sha256:')
  }
  runtimeIdentity: identities.outputs.runtimeIdentity
  quantRuntimeIdentity: identities.outputs.quantRuntimeIdentity
  containerRegistry: containerRegistry.outputs.registry
  roleAssignments: access.outputs.runtimeRoleAssignments
  quantRoleAssignments: access.outputs.quantRoleAssignments
  keyVaultNetwork: keyVault.outputs.networkAccess
  containerApps: containerApps.outputs
  costControls: costControls.outputs
  quantCostControls: {
    cryptoComputeEnabled: enableCryptoTrader
    rthTraderMaxReplicas: rthTraderReplicas
    backtestReplicaTimeoutSeconds: backtestReplicaTimeoutSeconds
    logRetentionDays: logRetentionDays
    monthlyBudgetAlertsUsd: costControls.outputs.monthlyBudgetAlerts.amountsUsd
  }
  thesis: {
    enabled: deployThesis
    api: deployThesis ? thesisApi.outputs.thesisApi : null
    identity: deployThesis ? thesisApi.outputs.thesisIdentity : null
    workspaces: deployThesis ? thesisWorkspaces.outputs.isolation : null
    costEnvelope: deployThesis ? thesisApi.outputs.costEnvelope : null
    image: {
      reference: thesisImageReference
      pinnedByDigest: contains(thesisImageReference, '@sha256:')
    }
  }
}
