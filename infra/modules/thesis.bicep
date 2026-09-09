targetScope = 'resourceGroup'

// Thesis API Container App
// ------------------------
// The Thesis backend reuses the established market-lake Container Apps
// pattern: a digest-pinned private ACR image pulled with a user-assigned
// managed identity, Key Vault secret references instead of secret values,
// /health probes, KEDA scale-to-zero, and diagnostics into the existing
// shared Log Analytics workspace. The Thesis image is built from
// Dockerfile.thesis with ENTRYPOINT "thesis", so the revision runs
// `thesis serve`. The serve process is currently a scaffold; deployment
// keeps the app degraded until the runtime lands, mirroring the harness
// trader posture.
//
// The managed identity is a parameter, not a new identity: the composition
// (infra/main.bicep) owns identity creation and wiring so the Thesis
// runtime identity is distinct from the market-lake runtime identity and
// the quant-harness identity, keeping provider, broker, and model secrets
// out of Thesis scope (fca-thesis.md security invariants).
//
// Additive monthly cost envelope (per the cost-controls.bicep style,
// Consumption plan list pricing): scale-to-zero by default means $0 idle
// compute. Each replica at 0.25 vCPU + 0.5 GiB for 730 hours costs roughly
// $12 per month, so the maxReplicas parameter bounds worst-case Thesis
// compute at about $12 x maxReplicas per month (about $24 at the default).
// Logs stream into the existing shared PerGB2018 workspace, so diagnostics
// add ingestion-only cost under the resource-group budget alerts.

@description('Name of the Thesis API Container App.')
@minLength(2)
param thesisApiAppName string

@description('Resource ID of the existing consumption Container Apps environment shared with the market-lake and quant-harness workloads.')
@minLength(1)
param containerAppsEnvironmentResourceId string

@description('Resource ID of the existing Log Analytics workspace that receives Thesis diagnostics.')
@minLength(1)
param logAnalyticsWorkspaceResourceId string

@description('Immutable digest-pinned private ACR image reference for the Thesis backend image built from Dockerfile.thesis.')
@minLength(1)
param thesisImageReference string

@description('Private Azure Container Registry login server. The Thesis app authenticates image pulls with its managed identity.')
@minLength(1)
param registryLoginServer string

@description('Versionless URI of the Key Vault that owns the Thesis API bearer token secret. No secret values are embedded in this module.')
@minLength(1)
param keyVaultUri string

@description('Full resource ID of the Thesis runtime user-assigned managed identity supplied by the composition.')
@minLength(1)
param thesisIdentityResourceId string

@description('Client ID of the Thesis runtime user-assigned managed identity. Exposed to Azure Identity as non-secret process configuration.')
@minLength(36)
@maxLength(36)
param thesisIdentityClientId string

@description('Principal (object) ID of the Thesis runtime user-assigned managed identity used for role assignments.')
@minLength(36)
@maxLength(36)
param thesisIdentityPrincipalId string

@description('Storage account name dedicated to Thesis workspace persistence, provisioned by thesis-workspaces.bicep.')
@minLength(3)
param thesisWorkspaceStorageAccountName string

@description('Blob container prefix every Thesis workspace container must use, exported by thesis-workspaces.bicep.')
@minLength(1)
param thesisWorkspaceContainerPrefix string

@description('Name of the workspace registry table on the Thesis persistence account.')
@minLength(1)
param thesisWorkspaceRegistryTableName string

@description('Azure region for the Thesis API Container App.')
param location string = resourceGroup().location

@description('Tags applied to the Thesis API Container App and its diagnostic setting.')
param tags object = {}

@description('Whether the Thesis API accepts public ingress. The secure default keeps the API internal; enable explicitly only when the authentication boundary is ready.')
param enableExternalIngress bool = false

@description('Entra application client ID the Thesis API accepts as token audience. Empty keeps the API on static bearer authentication only.')
param thesisApiEntraAudience string = ''

@description('CPU cores per Thesis API replica. Container Apps consumption allows 0.25 to 2.0 in 0.25 increments.')
@allowed([
  '0.25'
  '0.5'
  '0.75'
  '1.0'
  '1.25'
  '1.5'
  '1.75'
  '2.0'
])
param thesisContainerCpu string = '0.25'

@description('Memory per Thesis API replica. Must pair with the CPU choice per Container Apps consumption plan limits.')
@allowed([
  '0.5Gi'
  '1Gi'
  '1.5Gi'
  '2Gi'
  '2.5Gi'
  '3Gi'
  '3.5Gi'
  '4Gi'
])
param thesisContainerMemory string = '0.5Gi'

@description('Minimum replicas for the Thesis API. Zero scales the app to zero between requests; the deployment smoke may temporarily raise it with an exit-trapped restore.')
@minValue(0)
@maxValue(4)
param thesisMinReplicas int = 0

@description('Maximum replicas for the Thesis API. This value bounds the additive monthly cost envelope.')
@minValue(1)
@maxValue(4)
param thesisMaxReplicas int = 2

// Replica bounds are enforced with minValue/maxValue decorators; operators
// must keep thesisMinReplicas at or below thesisMaxReplicas because assert
// declarations still require an experimental Bicep feature.

var thesisContainerPort = 8000
var thesisContainerName = 'thesis-api'
var thesisSecretName = 'thesis-api-bearer-token'
var thesisSecrets = [
  {
    name: thesisSecretName
    keyVaultUrl: '${keyVaultUri}secrets/${thesisSecretName}'
    identity: thesisIdentityResourceId
  }
]
// The bearer token reaches the container only through a secretRef; no
// provider, model, or broker secret is configured for Thesis.
var thesisBaseEnvironment = [
  {
    name: 'AZURE_CLIENT_ID'
    value: thesisIdentityClientId
  }
  {
    name: 'THESIS_STORAGE_ACCOUNT_NAME'
    value: thesisWorkspaceStorageAccountName
  }
  {
    name: 'THESIS_WORKSPACE_CONTAINER_PREFIX'
    value: thesisWorkspaceContainerPrefix
  }
  {
    name: 'THESIS_WORKSPACE_REGISTRY_TABLE'
    value: thesisWorkspaceRegistryTableName
  }
]
// Entra configuration is non-secret; both variables are set together only
// when an audience is configured. An empty audience keeps the API on the
// static bearer token for local development and transition.
var thesisEntraEnvironment = empty(thesisApiEntraAudience) ? [] : [
  {
    name: 'THESIS_API__ENTRA_TENANT_ID'
    value: tenant().tenantId
  }
  {
    name: 'THESIS_API__ENTRA_AUDIENCE'
    value: thesisApiEntraAudience
  }
]
var thesisApiEnvironment = concat(thesisBaseEnvironment, thesisEntraEnvironment, [
  {
    name: 'THESIS_API__BEARER_TOKEN'
    secretRef: thesisSecretName
  }
])

// The vault is referenced, not created: the composition owns the Key Vault.
var keyVaultName = split(replace(keyVaultUri, 'https://', ''), '.')[0]

resource thesisKeyVault 'Microsoft.KeyVault/vaults@2026-02-01' existing = {
  name: keyVaultName
}

resource keyVaultSecretsUserRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' existing = {
  scope: subscription()
  name: '4633458b-17de-408a-b874-0445c86b69e6'
}

resource thesisSecretAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: thesisKeyVault
  name: guid(thesisKeyVault.id, thesisIdentityPrincipalId, keyVaultSecretsUserRole.id)
  properties: {
    principalId: thesisIdentityPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: keyVaultSecretsUserRole.id
  }
}

resource thesisApiApp 'Microsoft.App/containerApps@2025-07-01' = {
  name: thesisApiAppName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${thesisIdentityResourceId}': {}
    }
  }
  properties: {
    environmentId: containerAppsEnvironmentResourceId
    configuration: {
      activeRevisionsMode: 'Single'
      registries: [
        {
          server: registryLoginServer
          identity: thesisIdentityResourceId
        }
      ]
      secrets: thesisSecrets
      identitySettings: [
        {
          identity: thesisIdentityResourceId
          lifecycle: 'Main'
        }
      ]
      ingress: {
        external: enableExternalIngress
        allowInsecure: false
        targetPort: thesisContainerPort
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
          name: thesisContainerName
          image: thesisImageReference
          // Docker ENTRYPOINT remains "thesis"; args replace CMD ["--help"].
          args: [
            'serve'
          ]
          env: thesisApiEnvironment
          resources: {
            cpu: json(thesisContainerCpu)
            memory: thesisContainerMemory
          }
          probes: [
            {
              type: 'Startup'
              httpGet: {
                path: '/health'
                port: thesisContainerPort
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
                port: thesisContainerPort
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
                port: thesisContainerPort
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
        minReplicas: thesisMinReplicas
        maxReplicas: thesisMaxReplicas
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

resource thesisApiDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'logs-to-workspace'
  scope: thesisApiApp
  properties: {
    logAnalyticsDestinationType: 'Dedicated'
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
      }
    ]
    workspaceId: logAnalyticsWorkspaceResourceId
  }
}

@description('Operations surface of the deployed Thesis API. The digest-pinned image reference is reported so the smoke can verify immutability.')
output thesisApi object = {
  name: thesisApiApp.name
  resourceId: thesisApiApp.id
  fqdn: thesisApiApp.properties.configuration.ingress.fqdn
  containerPort: thesisContainerPort
  imageReference: thesisImageReference
  pinnedByDigest: contains(thesisImageReference, '@sha256:')
  ingressExternal: enableExternalIngress
}

@description('Identity wiring consumed by the Thesis API, echoed for non-secret composition summaries.')
output thesisIdentity object = {
  resourceId: thesisIdentityResourceId
  clientId: thesisIdentityClientId
}

@description('Additive cost envelope reported with the same spirit as cost-controls.bicep runtime guardrails.')
output costEnvelope object = {
  compute: {
    scaling: 'scale-to-zero by default'
    minReplicas: thesisMinReplicas
    maxReplicas: thesisMaxReplicas
    cpuCoresPerReplica: json(thesisContainerCpu)
    memoryPerReplica: thesisContainerMemory
    worstCaseMonthlyUsdPerReplica: 12
    idleMonthlyUsd: 0
  }
  persistence: {
    sku: 'Standard_LRS'
    idleMonthlyUsd: 0
    perGbMonthlyUsd: json('0.02')
  }
  observability: {
    workspace: 'existing shared PerGB2018 Log Analytics workspace'
    ingestionOnly: true
  }
}
