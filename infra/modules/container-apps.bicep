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
var quantApiSecrets = [
  {
    name: 'harness-api-bearer-token'
    keyVaultUrl: '${keyVaultUri}secrets/harness-api-bearer-token'
    identity: quantIdentityResourceId
  }
]
// Paper and live Alpaca trading endpoints share one key pair, so the
// credential mode does not change which Key Vault secrets the traders read.
var rthTraderSecrets = traderSecrets
var cryptoTraderSecrets = traderSecrets
var quantBaseEnvironment = [
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
var quantApiEnvironment = concat(quantBaseEnvironment, [
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
var backtestEnvironment = concat(quantBaseEnvironment, [
  {
    name: 'LAKE_API_URL'
    value: 'http://${apiApp.properties.configuration.ingress.fqdn}:${apiContainerPort}'
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
          command: [
            'api'
          ]
          args: [
            '--config'
            'config/harness/api.yaml'
            '--host'
            '0.0.0.0'
            '--port'
            string(quantContainerPort)
          ]
          env: quantApiEnvironment
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
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
      replicaRetryLimit: 1
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
          // The `backtest-job` console script execs the Rust engine binary.
          // The bounded synthetic smoke configuration ships inside the shared
          // image and the job runs it verbatim. Per-run argument overrides are
          // not possible here: az containerapp job start --args cannot pass
          // leading-dash tokens and its --yaml override drops container args,
          // so future per-run backtests need a different transport (env-var
          // configuration or a dedicated job).
          command: [
            'backtest-job'
          ]
          args: [
            '--config'
            'config/harness/backtest.yaml'
          ]
          env: backtestEnvironment
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
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
  port: apiContainerPort
}

@description('Use this name or resource ID with the Container Apps job start operation for an on-demand ingest execution.')
output ingest object = {
  name: ingestJob.name
  resourceId: ingestJob.id
  cronExpression: ingestCronExpression
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
