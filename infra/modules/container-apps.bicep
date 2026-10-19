targetScope = 'resourceGroup'

@description('Name of the Log Analytics workspace that receives Container Apps logs through Azure Monitor diagnostics.')
@minLength(4)
@maxLength(63)
param logAnalyticsWorkspaceName string

@description('Name of the consumption Container Apps environment.')
@minLength(2)
param containerAppsEnvironmentName string

@description('Immutable private ACR image reference used by both workloads.')
@minLength(1)
param sharedImageReference string

@description('Digest-pinned image containing only the finite ingestion worker.')
param ingestionImageReference string
@description('vCPU per finite ingestion Job execution on the Consumption profile.')
@allowed(['4'])
param ingestionCpu string = '4'
@description('GiB per finite ingestion Job execution on the Consumption profile.')
@allowed(['8'])
param ingestionMemoryGiB string = '8'
@minValue(1)
@maxValue(16)
param ingestionSessionConcurrency int = 2
param ingestionPipelineLimits object = {
  cpuSlots: 4
  responseMemoryMiB: 1024
  maxResponseMiB: 4
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

@description('Private Azure Container Registry login server. Both workloads authenticate image pulls with the runtime managed identity.')
@minLength(1)
param registryLoginServer string

@description('Versionless URI of the Key Vault that owns runtime provider and API secrets.')
@minLength(1)
param keyVaultUri string

@description('Storage account name used by the Azure ingest composition.')
@minLength(3)
param storageAccountName string

@description('Full resource ID of the runtime user-assigned managed identity.')
@minLength(1)
param runtimeIdentityResourceId string

@description('Client ID of the runtime user-assigned managed identity. Exposed to Azure Identity as non-secret process configuration.')
@minLength(36)
@maxLength(36)
param runtimeIdentityClientId string

@description('Azure region for the workspace and Container Apps resources.')
param location string = resourceGroup().location

@description('Tags applied to the workspace and Container Apps resources.')
param tags object = {}

@description('Retention in days for the Log Analytics workspace.')
@minValue(4)
@maxValue(730)
param logRetentionDays int = 30

@description('Whether Log Analytics queries may use its public endpoint. Azure Monitor diagnostic settings use a secure Microsoft channel, so public ingestion remains disabled; local key authentication is always disabled.')
param allowPublicLogQuery bool = false

@description('Whether the API and environment accept public ingress. The secure default keeps the API internal; enable explicitly only when the API authentication boundary is ready.')
param enableExternalApiIngress bool = false

@description('Entra tenant ID whose JWKS validates API caller tokens. Passed through unchanged from the deployment tenant.')
@minLength(1)
param lakeApiTenantId string

@description('Entra application client ID the API accepts as token audience. Empty keeps the API on static bearer authentication only.')
param lakeApiAudience string = ''

@description('Name of the scale-to-zero quant-harness API Container App.')
@minLength(2)
param quantApiAppName string

@description('Name of the scheduled RTH quant-harness trader Container App.')
@minLength(2)
param rthTraderAppName string

@description('Name of the optional always-on crypto quant-harness trader Container App.')
@minLength(2)
param cryptoTraderAppName string

@description('Name of the manually triggered quant-harness backtest Container Apps job.')
@minLength(1)
param backtestJobName string

@description('Full resource ID of the quant-harness user-assigned managed identity.')
@minLength(1)
param quantIdentityResourceId string

@description('Client ID of the quant-harness user-assigned managed identity. Exposed to Azure Identity as non-secret process configuration.')
@minLength(36)
@maxLength(36)
param quantIdentityClientId string

@description('Whether the always-on crypto trader Container App is deployed at all. False leaves no crypto compute in the resource group.')
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

@description('Replica count the RTH schedule rule applies while the session window is open. Outside the window the app scales back to zero.')
@minValue(1)
@maxValue(4)
param rthTraderReplicas int = 1

@description('Maximum seconds allowed for one backtest job replica execution. Per-run container argument overrides supply the backtest configuration at start time.')
@minValue(60)
@maxValue(86400)
param backtestReplicaTimeoutSeconds int = 3600

var consumptionProfileName = 'Consumption'
// Quant-harness workloads share the Container Apps environment and Log
// Analytics workspace with the lake but run under the dedicated quant
// identity and consume their own Key Vault secret names.
var quantContainerPort = 8000
var quantApiContainerName = 'api'
var traderContainerName = 'trader'
var backtestContainerName = 'backtest'
var harnessResultsContainerName = 'harness-results'
var harnessStateContainerName = 'harness-state'
var killSwitchTableName = 'killswitch'
var traderSecrets = [
  {
    name: 'alpaca-api-key'
    keyVaultUrl: '${keyVaultUri}secrets/alpaca-api-key'
    identity: quantIdentityResourceId
  }
  {
    name: 'alpaca-api-secret'
    keyVaultUrl: '${keyVaultUri}secrets/alpaca-api-secret'
    identity: quantIdentityResourceId
  }
]
var quantApiSecrets = concat(backtestSecrets, [
  {
    name: 'harness-api-bearer-token'
    keyVaultUrl: '${keyVaultUri}secrets/harness-api-bearer-token'
    identity: quantIdentityResourceId
  }
])
// Paper and live Alpaca trading endpoints share one key pair, so the
// credential mode does not change which Key Vault secrets the traders read.
var rthTraderSecrets = traderSecrets
var cryptoTraderSecrets = traderSecrets
var quantBaseEnvironment = [
  { name: 'CARGO_BUILD_JOBS', value: '2' }
  { name: 'HARNESS_EXECUTION_MAX_WORKERS', value: '2' }
  { name: 'HARNESS_EXECUTION_IMAGE', value: sharedImageReference }
  { name: 'HARNESS_EXECUTION_JOB_RESOURCE_ID', value: resourceId('Microsoft.App/jobs', backtestJobName) }

  {
    name: 'AZURE_CLIENT_ID'
    value: quantIdentityClientId
  }
  {
    name: 'LAKE_STORAGE_ACCOUNT_NAME'
    value: storageAccountName
  }
  {
    name: 'HARNESS_RESULTS_CONTAINER'
    value: harnessResultsContainerName
  }
  {
    name: 'HARNESS_STATE_CONTAINER'
    value: harnessStateContainerName
  }
  {
    name: 'HARNESS_KILL_SWITCH_TABLE'
    value: killSwitchTableName
  }
]
var ingestionPipelineEnvironment = [
  { name: 'LAKE_INGEST_CPU_SLOTS', value: string(ingestionPipelineLimits.cpuSlots) }
  { name: 'LAKE_INGEST_RESPONSE_MEMORY_MIB', value: string(ingestionPipelineLimits.responseMemoryMiB) }
  { name: 'LAKE_INGEST_MAX_RESPONSE_MIB', value: string(ingestionPipelineLimits.maxResponseMiB) }
  { name: 'LAKE_INGEST_MEMORY_MIB', value: string(ingestionPipelineLimits.memoryMiB) }
  { name: 'LAKE_INGEST_SESSION_MIB', value: string(ingestionPipelineLimits.sessionMiB) }
  { name: 'LAKE_INGEST_SCRATCH_MIB', value: string(ingestionPipelineLimits.scratchMiB) }
  { name: 'LAKE_INGEST_PUBLICATION_CONCURRENCY', value: string(ingestionPipelineLimits.publicationConcurrency) }
]
var ingestionEnvironment = concat([
  { name: 'HARNESS_MARKET_DATA_MODE', value: 'azure' }
  { name: 'HARNESS_MARKET_DATA_CACHE_MEMORY_BYTES', value: '134217728' }
  { name: 'HARNESS_MARKET_DATA_CACHE_DISK_BYTES', value: '1073741824' }
  { name: 'AZURE_SUBSCRIPTION_ID', value: subscription().subscriptionId }
  { name: 'LAKE_STORAGE_CONTAINER', value: 'lake' }
  { name: 'LAKE_COVERAGE_TABLE', value: 'coverage' }
  { name: 'LAKE_INGEST_IMAGE', value: ingestionImageReference }
  { name: 'LAKE_INGEST_RESOURCE_GROUP', value: resourceGroup().name }
  { name: 'LAKE_INGEST_JOB_NAME', value: '${quantApiAppName}-ingest' }
  { name: 'LAKE_INGEST_WORKER_CLIENT_ID', value: runtimeIdentityClientId }
  { name: 'LAKE_INGEST_CPU', value: ingestionCpu }
  { name: 'LAKE_INGEST_MEMORY_GIB', value: ingestionMemoryGiB }
  { name: 'LAKE_INGEST_SESSION_CONCURRENCY', value: string(ingestionSessionConcurrency) }
  { name: 'LAKE_INGEST_MAX_WORKERS', value: string(ingestionMaxWorkers) }
  { name: 'LAKE_INGEST_PARTITION_DAYS', value: string(ingestionPartitionDays) }
  { name: 'LAKE_INGEST_TIMEOUT_SECONDS', value: string(ingestionTimeoutSeconds) }
  { name: 'LAKE_PROVIDER_KEY_VAULT_URL', value: keyVaultUri }
], ingestionPipelineEnvironment)
var quantApiEnvironment = concat(backtestEnvironment, ingestionEnvironment, empty(lakeApiAudience) ? [] : [
  { name: 'LAKE_API__AUTH__ENTRA_TENANT_ID', value: lakeApiTenantId }
  { name: 'LAKE_API__AUTH__ENTRA_AUDIENCE', value: lakeApiAudience }
], [
  {
    name: 'HARNESS_API_BEARER_TOKEN'
    secretRef: 'harness-api-bearer-token'
  }
])
var rthTraderEnvironment = concat(quantBaseEnvironment, [
  {
    name: 'HARNESS_ALPACA_API_KEY'
    secretRef: 'alpaca-api-key'
  }
  {
    name: 'HARNESS_ALPACA_API_SECRET'
    secretRef: 'alpaca-api-secret'
  }
])
var cryptoTraderEnvironment = concat(quantBaseEnvironment, [
  {
    name: 'HARNESS_ALPACA_API_KEY'
    secretRef: 'alpaca-api-key'
  }
  {
    name: 'HARNESS_ALPACA_API_SECRET'
    secretRef: 'alpaca-api-secret'
  }
])
// The backtest job reads its run window through the frozen market-lake HTTP
// routes (route-only reads, no local lake path), so it carries the internal
// API endpoint and the lake bearer token under the quant identity.
var backtestSecrets = [
  {
    name: 'api-bearer-token'
    keyVaultUrl: '${keyVaultUri}secrets/api-bearer-token'
    identity: quantIdentityResourceId
  }
]
// Both ingress modes terminate TLS on 443. Internal app FQDNs include the
// internal segment; the container target port is never the ingress port.
var apiBaseUrl = enableExternalApiIngress
  ? 'https://${quantApiAppName}.${containerAppsEnvironment.properties.defaultDomain}/market-data'
  : 'https://${quantApiAppName}.internal.${containerAppsEnvironment.properties.defaultDomain}/market-data'
var backtestEnvironment = concat(quantBaseEnvironment, [
  {
    name: 'LAKE_API_URL'
    value: apiBaseUrl
  }
  {
    name: 'LAKE_API__BEARER_TOKEN'
    secretRef: 'api-bearer-token'
  }
])

resource logAnalyticsWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsWorkspaceName
  location: location
  tags: tags
  properties: {
    features: {
      disableLocalAuth: true
      enableLogAccessUsingOnlyResourcePermissions: true
    }
    publicNetworkAccessForIngestion: 'Disabled'
    publicNetworkAccessForQuery: allowPublicLogQuery ? 'Enabled' : 'Disabled'
    retentionInDays: logRetentionDays
    sku: {
      name: 'PerGB2018'
    }
  }
}

resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2025-07-01' = {
  name: containerAppsEnvironmentName
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'azure-monitor'
    }
    peerAuthentication: {
      mtls: {
        enabled: true
      }
    }
    peerTrafficConfiguration: {
      encryption: {
        enabled: true
      }
    }
    publicNetworkAccess: enableExternalApiIngress ? 'Enabled' : 'Disabled'
    workloadProfiles: [
      {
        name: consumptionProfileName
        workloadProfileType: 'Consumption'
      }
    ]
  }
}

resource environmentDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'logs-to-workspace'
  scope: containerAppsEnvironment
  properties: {
    logAnalyticsDestinationType: 'Dedicated'
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
      }
    ]
    workspaceId: logAnalyticsWorkspace.id
  }
}

// Coordination is finite and provider-free. The API can scale to zero without stranding durable work.
resource ingestionDispatcher 'Microsoft.App/jobs@2025-07-01' = {
  name: '${quantApiAppName}-dispatch'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${quantIdentityResourceId}': {} }
  }
  properties: {
    environmentId: containerAppsEnvironment.id
    workloadProfileName: consumptionProfileName
    configuration: {
      triggerType: 'Schedule'
      scheduleTriggerConfig: { cronExpression: '* * * * *', parallelism: 1, replicaCompletionCount: 1 }
      replicaRetryLimit: 0
      replicaTimeout: 600
      registries: [{ server: registryLoginServer, identity: quantIdentityResourceId }]
      identitySettings: [{ identity: quantIdentityResourceId, lifecycle: 'Main' }]
    }
    template: {
      containers: [{
        name: 'dispatcher'
        image: ingestionImageReference
        command: ['/bin/sh', '-c']
        args: ['export IDENTITY_ENDPOINT="$IDENTITY_ENDPOINT?client_id=$AZURE_CLIENT_ID"; exec /usr/local/bin/lake-ingest-dispatcher']
        env: concat(quantBaseEnvironment, ingestionEnvironment)
        resources: { cpu: json('0.25'), memory: '0.5Gi' }
      }]
    }
  }
}

// Only the finite worker owns provider acquisition and the provider credential
// identity. The coordinator starts executions of this fixed job definition.
resource ingestionJob 'Microsoft.App/jobs@2025-07-01' = {
  name: '${quantApiAppName}-ingest'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${runtimeIdentityResourceId}': {} }
  }
  properties: {
    environmentId: containerAppsEnvironment.id
    workloadProfileName: consumptionProfileName
    configuration: {
      triggerType: 'Manual'
      manualTriggerConfig: { parallelism: 1, replicaCompletionCount: 1 }
      replicaRetryLimit: 0
      replicaTimeout: ingestionTimeoutSeconds
      registries: [{ server: registryLoginServer, identity: runtimeIdentityResourceId }]
      identitySettings: [{ identity: runtimeIdentityResourceId, lifecycle: 'Main' }]
    }
    template: {
      containers: [{
        name: 'ingest'
        image: ingestionImageReference
        command: ['/usr/local/bin/lake-ingest-worker']
        env: concat([
          { name: 'AZURE_CLIENT_ID', value: runtimeIdentityClientId }
          { name: 'LAKE_STORAGE_ACCOUNT_NAME', value: storageAccountName }
          { name: 'LAKE_STORAGE_CONTAINER', value: 'lake' }
          { name: 'LAKE_COVERAGE_TABLE', value: 'coverage' }
          { name: 'LAKE_PROVIDER_KEY_VAULT_URL', value: keyVaultUri }
          { name: 'LAKE_INGEST_SESSION_CONCURRENCY', value: string(ingestionSessionConcurrency) }
          { name: 'LAKE_INGEST_TIMEOUT_SECONDS', value: string(ingestionTimeoutSeconds) }
        ], ingestionPipelineEnvironment)
        resources: { cpu: json(ingestionCpu), memory: '${ingestionMemoryGiB}Gi' }
      }]
    }
  }
}

resource quantApiApp 'Microsoft.App/containerApps@2025-07-01' = {
  name: quantApiAppName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${quantIdentityResourceId}': {}
    }
  }
  properties: {
    environmentId: containerAppsEnvironment.id
    workloadProfileName: consumptionProfileName
    configuration: {
      activeRevisionsMode: 'Single'
      registries: [
        {
          server: registryLoginServer
          identity: quantIdentityResourceId
        }
      ]
      secrets: quantApiSecrets
      identitySettings: [
        {
          identity: quantIdentityResourceId
          lifecycle: 'Main'
        }
      ]
      ingress: {
        external: enableExternalApiIngress
        allowInsecure: false
        targetPort: quantContainerPort
        transport: 'http'
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
    }
    template: {
      containers: [
        {
          name: quantApiContainerName
          image: sharedImageReference
          // The harness console scripts are sibling entrypoints in the shared
          // image. The `api` console script execs the Rust axum boundary
          // directly; the `harness` facade no longer fronts every group.
          command: ['/bin/sh', '-c']
          args: ['export IDENTITY_ENDPOINT="$IDENTITY_ENDPOINT?client_id=$AZURE_CLIENT_ID"; exec api --config config/harness/api.yaml --host 0.0.0.0 --port ${quantContainerPort}']
          env: quantApiEnvironment
          resources: {
            cpu: json('1.0')
            memory: '2Gi'
          }
          probes: [
            {
              type: 'Startup'
              httpGet: {
                path: '/health'
                port: quantContainerPort
                scheme: 'HTTP'
              }
              failureThreshold: 10
              initialDelaySeconds: 1
              periodSeconds: 3
              timeoutSeconds: 1
            }
            {
              type: 'Liveness'
              httpGet: {
                path: '/health'
                port: quantContainerPort
                scheme: 'HTTP'
              }
              failureThreshold: 3
              periodSeconds: 30
              timeoutSeconds: 2
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/health'
                port: quantContainerPort
                scheme: 'HTTP'
              }
              failureThreshold: 3
              periodSeconds: 10
              timeoutSeconds: 2
            }
          ]
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 2
        rules: [
          {
            name: 'http-requests'
            http: {
              metadata: {
                concurrentRequests: '25'
              }
            }
          }
        ]
      }
    }
  }
}

resource rthTraderApp 'Microsoft.App/containerApps@2025-07-01' = {
  name: rthTraderAppName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${quantIdentityResourceId}': {}
    }
  }
  properties: {
    environmentId: containerAppsEnvironment.id
    workloadProfileName: consumptionProfileName
    configuration: {
      activeRevisionsMode: 'Single'
      registries: [
        {
          server: registryLoginServer
          identity: quantIdentityResourceId
        }
      ]
      secrets: rthTraderSecrets
      identitySettings: [
        {
          identity: quantIdentityResourceId
          lifecycle: 'Main'
        }
      ]
    }
    template: {
      containers: [
        {
          name: traderContainerName
          image: sharedImageReference
          // The `trader` console script execs the Rust trader binary. The
          // `harness` facade does not dispatch the deferred trader group, so
          // the workload must call the entrypoint directly.
          command: [
            'trader'
          ]
          env: rthTraderEnvironment
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: rthTraderReplicas
        rules: [
          {
            name: 'rth-schedule'
            // The cron scaler is a KEDA custom scaler; the API models it
            // through custom.type rather than a first-class cron property.
            custom: {
              type: 'cron'
              metadata: {
                timezone: rthScheduleTimezone
                start: rthScheduleStart
                end: rthScheduleEnd
                desiredReplicas: string(rthTraderReplicas)
              }
            }
          }
        ]
      }
    }
  }
}

resource cryptoTraderApp 'Microsoft.App/containerApps@2025-07-01' = if (enableCryptoTrader) {
  name: cryptoTraderAppName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${quantIdentityResourceId}': {}
    }
  }
  properties: {
    environmentId: containerAppsEnvironment.id
    workloadProfileName: consumptionProfileName
    configuration: {
      activeRevisionsMode: 'Single'
      registries: [
        {
          server: registryLoginServer
          identity: quantIdentityResourceId
        }
      ]
      secrets: cryptoTraderSecrets
      identitySettings: [
        {
          identity: quantIdentityResourceId
          lifecycle: 'Main'
        }
      ]
    }
    template: {
      containers: [
        {
          name: traderContainerName
          image: sharedImageReference
          // The `trader` console script execs the Rust trader binary; see the
          // RTH trader above for why the facade is not used.
          command: [
            'trader'
          ]
          env: cryptoTraderEnvironment
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
        }
      ]
      // Crypto sessions run continuously, so the trader stays always-on at a
      // single bounded replica with no autoscaling rules.
      scale: {
        minReplicas: 1
        maxReplicas: 1
        rules: []
      }
    }
  }
}

// Finite, low-resource recovery control survives API scale-to-zero. Capacity
// hints may overlap; fenced queue claims prevent duplicate engine execution.
resource executionDispatcher 'Microsoft.App/jobs@2025-07-01' = {
  name: '${quantApiAppName}-execution-dispatch'
  location: location
  tags: tags
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${quantIdentityResourceId}': {} } }
  properties: {
    environmentId: containerAppsEnvironment.id
    workloadProfileName: consumptionProfileName
    configuration: {
      triggerType: 'Schedule'
      scheduleTriggerConfig: { cronExpression: '* * * * *', parallelism: 1, replicaCompletionCount: 1 }
      replicaRetryLimit: 0
      replicaTimeout: 120
      registries: [{ server: registryLoginServer, identity: quantIdentityResourceId }]
      secrets: backtestSecrets
      identitySettings: [{ identity: quantIdentityResourceId, lifecycle: 'Main' }]
    }
    template: {
      containers: [{
        name: 'dispatcher'
        image: sharedImageReference
        command: ['/bin/sh', '-c']
        args: ['export IDENTITY_ENDPOINT="$IDENTITY_ENDPOINT?client_id=$AZURE_CLIENT_ID"; exec harness-worker --dispatch']
        env: concat(backtestEnvironment, ingestionEnvironment)
        resources: { cpu: json('0.25'), memory: '0.5Gi' }
      }]
    }
  }
}

resource backtestJob 'Microsoft.App/jobs@2025-07-01' = {
  name: backtestJobName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${quantIdentityResourceId}': {}
    }
  }
  properties: {
    environmentId: containerAppsEnvironment.id
    workloadProfileName: consumptionProfileName
    configuration: {
      triggerType: 'Manual'
      registries: [
        {
          server: registryLoginServer
          identity: quantIdentityResourceId
        }
      ]
      manualTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
      }
      secrets: backtestSecrets
      replicaRetryLimit: 0
      replicaTimeout: backtestReplicaTimeoutSeconds
      identitySettings: [
        {
          identity: quantIdentityResourceId
          lifecycle: 'Main'
        }
      ]
    }
    template: {
      containers: [
        {
          name: backtestContainerName
          image: sharedImageReference
          // Every worker claims a durable accepted job; this template contains
          // no fixed smoke configuration and cannot invent an execution ID.
          command: ['/bin/sh', '-c']
          args: ['export IDENTITY_ENDPOINT="$IDENTITY_ENDPOINT?client_id=$AZURE_CLIENT_ID"; exec harness-worker']
          env: concat(backtestEnvironment, ingestionEnvironment)
          resources: {
            cpu: json('4.0')
            memory: '8Gi'
          }
        }
      ]
    }
  }
}

output logAnalyticsWorkspace object = {
  name: logAnalyticsWorkspace.name
  resourceId: logAnalyticsWorkspace.id
}

output containerAppsEnvironment object = {
  name: containerAppsEnvironment.name
  resourceId: containerAppsEnvironment.id
  workloadProfile: consumptionProfileName
}

output marketData object = {
  fqdn: quantApiApp.properties.configuration.ingress.fqdn
  basePath: '/market-data'
}

output ingestionCoordination object = {
  jobName: ingestionDispatcher.name
  image: ingestionImageReference
  schedule: '* * * * *'
}

output ingestion object = {
  jobName: ingestionJob.name
  resourceId: ingestionJob.id
  image: ingestionImageReference
  cpu: ingestionCpu
  memoryGiB: ingestionMemoryGiB
  maxWorkers: ingestionMaxWorkers
  partitionDays: ingestionPartitionDays
  sessionConcurrency: ingestionSessionConcurrency
}

@description('Scale-to-zero quant-harness results and job API.')
output quantApi object = {
  name: quantApiApp.name
  resourceId: quantApiApp.id
  fqdn: quantApiApp.properties.configuration.ingress.fqdn
}

@description('Scheduled RTH trader that scales from zero on the session cron window.')
output rthTrader object = {
  name: rthTraderApp.name
  resourceId: rthTraderApp.id
  schedule: {
    timezone: rthScheduleTimezone
    start: rthScheduleStart
    end: rthScheduleEnd
    replicas: rthTraderReplicas
  }
}

@description('Crypto trader resource. Empty when the crypto trader is disabled, in which case no crypto compute exists.')
output cryptoTrader object = {
  enabled: enableCryptoTrader
  name: enableCryptoTrader ? cryptoTraderApp.name : ''
  resourceId: enableCryptoTrader ? cryptoTraderApp.id : ''
}

@description('Use this name or resource ID with the Container Apps job start operation; supply the backtest configuration through the container argument override.')
output backtest object = {
  name: backtestJob.name
  resourceId: backtestJob.id
  replicaTimeoutSeconds: backtestReplicaTimeoutSeconds
}
