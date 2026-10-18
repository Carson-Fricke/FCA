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

@description('Digest-pinned finite Container Apps ingestion Job worker image.')
param ingestionImageReference string = sharedImageReference
@allowed(['4'])
param ingestionCpu string = '4'
@allowed(['8'])
param ingestionMemoryGiB string = '8'
@minValue(1)
@maxValue(16)
param ingestionSessionConcurrency int = 2
@description('Per-worker resource admission limits. Keep accounted session + response memory below the worker allocation, with runtime headroom.')
param ingestionPipelineLimits object = {
  cpuSlots: 4
  responseMemoryMiB: 1024
  maxResponseMiB: 8
  memoryMiB: 4096
  sessionMiB: 2048
  publicationConcurrency: 2
  scratchMiB: 4096
}
@minValue(1)
@maxValue(64)
param ingestionMaxWorkers int = 8
@minValue(1)
@maxValue(366)
param ingestionPartitionDays int = 31
@minValue(60)
@maxValue(604800)
param ingestionTimeoutSeconds int = 86400

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

@description('Entra application client ID the API accepts as token audience. Empty keeps the API on static bearer authentication only.')
param lakeApiAudience string = ''

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

@description('OpenRouter model name injected into the Thesis API as THESIS_MODEL. Empty uses the runtime default (deepseek/deepseek-v4.1-flash).')
param thesisModel string = ''

@description('Harness MCP endpoint for the Thesis workflow runtime. Empty derives the quant API HTTPS ingress endpoint.')
param thesisHarnessMcpUrl string = ''

@description('Key Vault secret name holding the quant API bearer token. Empty disables the Thesis workflow runtime.')
param thesisHarnessBearerTokenSecretName string = ''

@description('Key Vault secret name holding the OpenRouter API key (operator-provisioned). Empty disables model replies and the Thesis workflow runtime.')
param thesisModelApiKeySecretName string = ''

@description('Key Vault secret name holding the Azure Communication Services email connection string. Empty disables invite-mail delivery so the composition stays deployable before the operator seeds the secret; set it to enable mail.')
param thesisMailerConnectionStringSecretName string = ''

@description('Whether to self-host open-source Gitea with internal ingress and isolated Azure Files persistence. False creates no forge resources and preserves existing deployment behavior.')
param enableGitea bool = false

@description('Official rootful Gitea image reference pinned by SHA-256 digest, required when enableGitea is true. Empty, mutable, or non-official references fail closed on image pull.')
param giteaImageReference string = ''

@secure()
@description('Gitea PostgreSQL administrator password, resolved from Key Vault by deployment composition.')
param giteaDatabasePassword string = ''

@description('Existing self-hosted Gitea endpoint injected as THESIS_GITEA_URL when enableGitea is false. When enabled, the hosted endpoint takes precedence. Set organization and token secret name explicitly; all empty keeps local workspaces.')
param thesisGiteaUrl string = ''

@description('Operator-bootstrapped Gitea organization owning remote Thesis workspace repositories. Required with either the hosted or explicit Gitea endpoint and thesisGiteaTokenSecretName; never generated by the deployment.')
param thesisGiteaOrganization string = ''

@description('Name of the operator-provisioned Key Vault secret holding the Gitea token. Required with either the hosted or explicit Gitea endpoint and thesisGiteaOrganization; never pass the token value.')
param thesisGiteaTokenSecretName string = ''

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

// The public Thesis SPA host is additive and off by default, so a
// Thesis-enabled deployment that predates adr-001 remains byte-for-byte
// unchanged. The Static Web App is composed only when both deployThesis and
// deployThesisStaticWebApp are true. The linked backend and custom-domain
// binding are owned by a later task (T301).
@description('Whether to deploy the public Thesis SvelteKit Static Web App. False leaves the Thesis composition unchanged and keeps only the internal API.')
param deployThesisStaticWebApp bool = false

@description('Globally unique name of the Thesis Static Web App. Empty derives a deterministic name from the project prefix and resource group so no operator input is required.')
@maxLength(60)
param thesisStaticWebAppName string = ''

@description('Operator-owned custom hostname for the public Thesis site. Empty keeps the platform-generated Static Web App hostname; a non-empty value is carried to T301, which binds the hostname and managed certificate.')
param thesisStaticWebAppCustomDomain string = ''

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
var quantApiAppName = '${namePrefix}-quant-api'
var rthTraderAppName = '${namePrefix}-rth-trader'
var cryptoTraderAppName = '${namePrefix}-crypto-trader'
var backtestJobName = '${namePrefix}-backtest'
var thesisIdentityName = '${namePrefix}-thesis'
var thesisApiAppName = '${namePrefix}-thesis-api'
var giteaAppName = '${shortNamePrefix}-gitea'
var giteaStorageAccountName = 'gitea${take(deterministicSuffix, 18)}'
// Static Web App names are globally unique, so the derived fallback mixes the
// resource group id through deterministicSuffix instead of relying on the
// prefix alone. An explicit thesisStaticWebAppName always wins.
var thesisStaticWebAppResourceName = empty(thesisStaticWebAppName) ? '${shortNamePrefix}-thesis-web-${take(deterministicSuffix, 6)}' : thesisStaticWebAppName
// Static Web Apps do not support network-isolated (internal-ingress) linked
// backends, so enabling the public SPA requires the Thesis API on external
// ingress. The API stays default-deny: every /v1 route is authenticated and
// workspace-authorized. An operator can also enable external ingress directly.
var thesisIngressExternal = thesisEnableExternalIngress || deployThesisStaticWebApp
// A dedicated persistence account for Thesis workspaces: 'thesis' plus the
// same deterministic suffix keeps the name distinct from the lake account.
var thesisWorkspacesStorageAccountName = 'thesis${take(deterministicSuffix, 18)}'
// A dedicated Cosmos DB account for Thesis conversation state. The name stays
// globally unique through the deterministic suffix and follows Cosmos naming
// rules (lowercase letters, digits, hyphens).
var thesisCosmosAccountName = '${shortNamePrefix}-cosmos-${take(deterministicSuffix, 6)}'
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
    quantPrincipalId: identities.outputs.quantRuntimeIdentity.principalId
    runtimePrincipalId: identities.outputs.runtimeIdentity.principalId
    thesisPrincipalId: deployThesis ? thesisIdentity.properties.principalId : ''
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
    enableGitea: enableGitea
    runtimePrincipalId: identities.outputs.runtimeIdentity.principalId
    quantPrincipalId: identities.outputs.quantRuntimeIdentity.principalId
    githubDeployerPrincipalId: identities.outputs.githubDeployerIdentity.principalId
    githubDeployerIdentityName: githubDeployerIdentityName
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
    sharedImageReference: sharedImageReference
    ingestionImageReference: ingestionImageReference
    ingestionCpu: ingestionCpu
    ingestionMemoryGiB: ingestionMemoryGiB
    ingestionSessionConcurrency: ingestionSessionConcurrency
    ingestionPipelineLimits: ingestionPipelineLimits
    ingestionMaxWorkers: ingestionMaxWorkers
    ingestionPartitionDays: ingestionPartitionDays
    ingestionTimeoutSeconds: ingestionTimeoutSeconds
    registryLoginServer: containerRegistry.outputs.registry.loginServer
    storageAccountName: storage.outputs.storageAccountName
    keyVaultUri: keyVault.outputs.keyVaultUri
    runtimeIdentityResourceId: identities.outputs.runtimeIdentity.resourceId
    runtimeIdentityClientId: identities.outputs.runtimeIdentity.clientId
    location: location
    tags: tags
    logRetentionDays: logRetentionDays
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

module gitea './modules/gitea.bicep' = if (enableGitea) {
  name: 'workspace-gitea'
  params: {
    appName: giteaAppName
    storageAccountName: giteaStorageAccountName
    containerAppsEnvironmentResourceId: containerApps.outputs.containerAppsEnvironment.resourceId
    giteaImageReference: giteaImageReference
    databasePassword: giteaDatabasePassword
    location: location
    tags: tags
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

module thesisCosmos './modules/thesis-cosmos.bicep' = if (deployThesis) {
  name: 'thesis-cosmos'
  params: {
    accountName: thesisCosmosAccountName
    location: location
    tags: tags
    // Same conditional-identity guard as thesis-workspaces: the filler is
    // never evaluated in an enabled deployment.
    thesisPrincipalId: deployThesis ? thesisIdentity.properties.principalId : '00000000-0000-0000-0000-000000000000'
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
    thesisImageReference: thesisImageReference
    registryLoginServer: containerRegistry.outputs.registry.loginServer
    keyVaultUri: keyVault.outputs.keyVaultUri
    thesisIdentityResourceId: deployThesis ? thesisIdentity.id : '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/not-deployed'
    thesisIdentityClientId: deployThesis ? thesisIdentity.properties.clientId : '00000000-0000-0000-0000-000000000000'
    thesisIdentityPrincipalId: deployThesis ? thesisIdentity.properties.principalId : '00000000-0000-0000-0000-000000000000'
    thesisWorkspaceStorageAccountName: deployThesis ? thesisWorkspaces.outputs.storageAccountName : 'thesisnotdeployed'
    thesisWorkspaceContainerPrefix: deployThesis ? thesisWorkspaces.outputs.containerPrefix : 'thesis-ws-'
    thesisWorkspaceRegistryTableName: deployThesis ? thesisWorkspaces.outputs.registryTableName : 'workspaces'
    thesisCosmosEndpoint: deployThesis ? thesisCosmos.outputs.endpoint : 'https://not-deployed.invalid'
    thesisCosmosDatabaseName: deployThesis ? thesisCosmos.outputs.databaseName : 'thesis'
    // MCP is hosted by the quant API. ACA ingress terminates TLS on 443;
    // container target ports are never part of a service's ingress URL.
    thesisModel: thesisModel
    harnessMcpUrl: empty(thesisHarnessMcpUrl) ? 'https://${containerApps.outputs.quantApi.fqdn}' : thesisHarnessMcpUrl
    harnessBearerTokenSecretName: thesisHarnessBearerTokenSecretName
    modelApiKeySecretName: thesisModelApiKeySecretName
    mailerConnectionStringSecretName: thesisMailerConnectionStringSecretName
    giteaUrl: enableGitea ? gitea!.outputs.endpoint : thesisGiteaUrl
    giteaOrganization: thesisGiteaOrganization
    giteaTokenSecretName: thesisGiteaTokenSecretName
    location: location
    tags: tags
    enableExternalIngress: thesisIngressExternal
    thesisApiEntraAudience: thesisApiAudience
    customDomainName: thesisCustomDomain
    thesisContainerCpu: thesisContainerCpu
    thesisContainerMemory: thesisContainerMemory
    thesisMinReplicas: thesisMinReplicas
    thesisMaxReplicas: thesisMaxReplicas
  }
}

module thesisStaticWebApp './modules/static-web-app.bicep' = if (deployThesis && deployThesisStaticWebApp) {
  name: 'thesis-static-web-app'
  params: {
    staticWebAppName: thesisStaticWebAppResourceName
    location: location
    tags: tags
    customDomainName: thesisStaticWebAppCustomDomain
    thesisApiResourceId: deployThesis ? thesisApi.outputs.backend.resourceId : ''
    thesisApiRegion: deployThesis ? thesisApi.outputs.backend.region : location
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
    cosmos: deployThesis ? thesisCosmos.outputs.persistence : null
    costEnvelope: deployThesis ? thesisApi.outputs.costEnvelope : null
    staticWebAppEnabled: deployThesis && deployThesisStaticWebApp
    staticWebApp: (deployThesis && deployThesisStaticWebApp) ? thesisStaticWebApp.outputs.staticWebApp : null
    image: {
      reference: thesisImageReference
      pinnedByDigest: contains(thesisImageReference, '@sha256:')
    }
  }
}
