targetScope = 'resourceGroup'

@description('Name of the Log Analytics workspace that receives Container Apps logs through Azure Monitor diagnostics.')
@minLength(4)
@maxLength(63)
param logAnalyticsWorkspaceName string

@description('Name of the consumption Container Apps environment.')
@minLength(2)
param containerAppsEnvironmentName string

@description('Name of the market-lake FastAPI Container App.')
@minLength(2)
param apiAppName string

@description('Name of the market-lake ingest Container Apps job.')
@minLength(1)
param ingestJobName string

@description('Immutable private ACR image reference used by both workloads.')
@minLength(1)
param sharedImageReference string

@description('Private Azure Container Registry login server. Both workloads authenticate image pulls with the runtime managed identity.')
@minLength(1)
param registryLoginServer string

@description('Versionless URI of the Key Vault that owns runtime provider and API secrets.')
@minLength(1)
param keyVaultUri string

@description('Storage account name used by the Azure ingest composition.')
@minLength(3)
param storageAccountName string

@description('Maximum Alpaca records fetched per data type for one bounded ingest execution.')
@minValue(1)
@maxValue(100000)
param ingestMaxRecords int = 1000

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

@description('Five-field UTC cron expression for scheduled ingest. A scheduled Container Apps job can also be started on demand.')
@minLength(9)
param ingestCronExpression string = '0 1 * * *'

@description('Maximum seconds allowed for one ingest replica execution.')
@minValue(60)
@maxValue(86400)
param ingestReplicaTimeoutSeconds int = 3600

@description('Whether Log Analytics queries may use its public endpoint. Azure Monitor diagnostic settings use a secure Microsoft channel, so public ingestion remains disabled; local key authentication is always disabled.')
param allowPublicLogQuery bool = false

@description('Whether the API and environment accept public ingress. The secure default keeps the API internal; enable explicitly only when the API authentication boundary is ready.')
param enableExternalApiIngress bool = false

@description('Entra tenant ID whose JWKS validates API caller tokens. Passed through unchanged from the deployment tenant.')
@minLength(1)
param lakeApiTenantId string

@description('Entra application client ID the API accepts as token audience. Empty keeps the API on static bearer authentication only.')
param lakeApiAudience string = ''

var consumptionProfileName = 'Consumption'
var apiContainerPort = 8000
var apiContainerName = 'api'
var ingestContainerName = 'ingest'
var providerSecrets = [
  {
    name: 'alpaca-api-key'
    keyVaultUrl: '${keyVaultUri}secrets/alpaca-api-key'
    identity: runtimeIdentityResourceId
  }
  {
    name: 'alpaca-api-secret'
    keyVaultUrl: '${keyVaultUri}secrets/alpaca-api-secret'
    identity: runtimeIdentityResourceId
  }
]
var apiSecrets = concat(providerSecrets, [
  {
    name: 'api-bearer-token'
    keyVaultUrl: '${keyVaultUri}secrets/api-bearer-token'
    identity: runtimeIdentityResourceId
  }
])
var baseRuntimeEnvironment = [
  {
    name: 'AZURE_CLIENT_ID'
    value: runtimeIdentityClientId
  }
  {
    name: 'LAKE_STORAGE_ACCOUNT_NAME'
    value: storageAccountName
  }
  {
    name: 'LAKE_STORAGE_CONTAINER'
    value: 'lake'
  }
  {
    name: 'LAKE_COVERAGE_TABLE'
    value: 'coverage'
  }
  {
    name: 'LAKE_INGEST_MAX_RECORDS'
    value: string(ingestMaxRecords)
  }
]
var providerEnvironment = concat(baseRuntimeEnvironment, [
  {
    name: 'LAKE_PROVIDER__API_KEY'
    secretRef: 'alpaca-api-key'
  }
  {
    name: 'LAKE_PROVIDER__API_SECRET'
    secretRef: 'alpaca-api-secret'
  }
])
// Entra configuration is non-secret; both variables are set together only
// when an audience is configured. An empty audience keeps the API on the
// static bearer token for local development and transition.
var apiEntraEnvironment = empty(lakeApiAudience) ? [] : [
  {
    name: 'LAKE_API__AUTH__ENTRA_TENANT_ID'
    value: lakeApiTenantId
  }
  {
    name: 'LAKE_API__AUTH__ENTRA_AUDIENCE'
    value: lakeApiAudience
  }
]
var apiEnvironment = concat(providerEnvironment, apiEntraEnvironment, [
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

resource apiApp 'Microsoft.App/containerApps@2025-07-01' = {
  name: apiAppName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${runtimeIdentityResourceId}': {}
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
          identity: runtimeIdentityResourceId
        }
      ]
      secrets: apiSecrets
      identitySettings: [
        {
          identity: runtimeIdentityResourceId
          lifecycle: 'Main'
        }
      ]
      ingress: {
        external: enableExternalApiIngress
        allowInsecure: false
        targetPort: apiContainerPort
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
          name: apiContainerName
          image: sharedImageReference
          // Docker ENTRYPOINT remains "lake"; args replace only CMD ["--help"].
          args: [
            'api'
          ]
          env: apiEnvironment
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          probes: [
            {
              type: 'Startup'
              httpGet: {
                path: '/health'
                port: apiContainerPort
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
                port: apiContainerPort
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
                port: apiContainerPort
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

resource ingestJob 'Microsoft.App/jobs@2025-07-01' = {
  name: ingestJobName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${runtimeIdentityResourceId}': {}
    }
  }
  properties: {
    environmentId: containerAppsEnvironment.id
    workloadProfileName: consumptionProfileName
    configuration: {
      triggerType: 'Schedule'
      registries: [
        {
          server: registryLoginServer
          identity: runtimeIdentityResourceId
        }
      ]
      secrets: providerSecrets
      scheduleTriggerConfig: {
        cronExpression: ingestCronExpression
        parallelism: 1
        replicaCompletionCount: 1
      }
      replicaRetryLimit: 1
      replicaTimeout: ingestReplicaTimeoutSeconds
      identitySettings: [
        {
          identity: runtimeIdentityResourceId
          lifecycle: 'Main'
        }
      ]
    }
    template: {
      containers: [
        {
          name: ingestContainerName
          image: sharedImageReference
          // Scheduled jobs retain their schedule when started manually.
          args: [
            'ingest'
          ]
          env: providerEnvironment
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
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

output api object = {
  name: apiApp.name
  resourceId: apiApp.id
  fqdn: apiApp.properties.configuration.ingress.fqdn
}

@description('Use this name or resource ID with the Container Apps job start operation for an on-demand ingest execution.')
output ingest object = {
  name: ingestJob.name
  resourceId: ingestJob.id
  cronExpression: ingestCronExpression
}
