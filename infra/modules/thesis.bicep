targetScope = 'resourceGroup'

// Thesis API Container App
// ------------------------
// The Thesis backend reuses the established market-lake Container Apps
// pattern: a digest-pinned private ACR image pulled with a user-assigned
// managed identity, Key Vault secret references instead of secret values,
// /health probes, KEDA scale-to-zero, and diagnostics into the existing
// shared Log Analytics workspace. The Thesis image is built from
// Dockerfile.thesis with ENTRYPOINT "thesis", so the revision runs
// `thesis serve`, which composes the real ASGI application. The serve host
// binds all interfaces inside the container so Container Apps ingress and the
// /health probes reach it.
//
// The managed identity is a parameter, not a new identity: the composition
// (infra/main.bicep) owns identity creation and wiring so the Thesis
// runtime identity is distinct from the market-lake runtime identity and
// the quant-harness identity, keeping provider, broker, and model secrets
// out of Thesis scope (fca-thesis.md security invariants).
//
// Durable conversation state lives in the Cosmos DB account provisioned by
// thesis-cosmos.bicep. That account disables local (key) auth, so the runtime
// authenticates with this managed identity through data-plane RBAC and no
// Cosmos secret exists in Key Vault or in this module.
//
// Public hosting on an operator-owned hostname is opt-in and has two mutually
// exclusive postures. The direct posture sets customDomainName (and requires
// enableExternalIngress=true) and binds the hostname to this Container App.
// The Static Web App posture leaves customDomainName empty, uses external
// ingress (Static Web Apps do not support network-isolated linked backends),
// and lets static-web-app.bicep own the public hostname and managed
// certificate; the composition passes this module's `backend` output to that
// module. The binding is gated on external ingress so a custom hostname can
// never be requested on internal ingress (Container Apps rejects it). The
// registrar steps stay with the operator (domain-hosting.md runbook); this
// module never holds a registrar credential or a certificate file.
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

@description('Endpoint of the Thesis Cosmos DB account consumed as THESIS_COSMOS_ENDPOINT, provisioned by thesis-cosmos.bicep. The runtime authenticates with the managed identity, so no key is injected.')
@minLength(1)
param thesisCosmosEndpoint string

@description('Cosmos SQL database name consumed as THESIS_COSMOS_DATABASE. Must match the database provisioned by thesis-cosmos.bicep.')
@minLength(1)
param thesisCosmosDatabaseName string = 'thesis'

@description('OpenRouter model name injected as THESIS_MODEL. Empty leaves the runtime default (deepseek/deepseek-v4.1-flash).')
param thesisModel string = ''

@description('Harness MCP endpoint injected as HARNESS_MCP_URL. Empty disables the workflow runtime; the composition derives the internal market-lake api endpoint by default.')
param harnessMcpUrl string = ''

@description('Name of the operator-provisioned Key Vault secret holding the harness API bearer token, injected as HARNESS_API_BEARER_TOKEN. Empty disables the workflow runtime so the module stays deployable before the secret exists.')
param harnessBearerTokenSecretName string = ''

@description('Name of the operator-provisioned Key Vault secret holding the OpenRouter API key, injected as OPENROUTER_API_KEY. Empty disables model replies and the workflow runtime.')
param modelApiKeySecretName string = ''

@description('Name of the operator-provisioned Key Vault secret holding the Azure Communication Services email connection string. No secret value is embedded in this module. Empty disables invite-mail delivery wiring so the module stays deployable before the operator creates the secret (see .github/runbooks/thesis-public-auth.md Step 4).')
param mailerConnectionStringSecretName string = 'thesis-acs-email-connection-string'

@description('Verified sender address used for invite emails, for example DoNotReply@<verified-domain>. Non-secret operator configuration injected into the Thesis Container App.')
param mailerSenderAddress string = ''

@description('Azure Communication Services endpoint URI used for invite emails. Empty lets the mailer derive it from the connection string. Non-secret operator configuration injected into the Thesis Container App.')
param mailerEndpoint string = ''

@description('Azure region for the Thesis API Container App.')
param location string = resourceGroup().location

@description('Tags applied to the Thesis API Container App and its diagnostic setting.')
param tags object = {}

@description('Whether the Thesis API accepts public ingress. The secure default keeps the API internal; enable explicitly only when the authentication boundary is ready.')
param enableExternalIngress bool = false

@description('Entra application client ID the Thesis API accepts as token audience. Empty keeps the API on static bearer authentication only.')
param thesisApiEntraAudience string = ''

@description('Optional public custom hostname for the direct Container App posture, for example thesis.<operator-domain>. Empty keeps the deployment exactly on the Container Apps default FQDN with no hostname binding and no managed certificate. A non-empty value requires enableExternalIngress=true and operator-created registrar DNS records (see .github/runbooks/domain-hosting.md). When the Static Web App is the public origin, leave this empty and set thesisStaticWebAppCustomDomain instead; the two must never claim the same hostname.')
param customDomainName string = ''

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
// The ACS connection string is operator-provisioned in Key Vault
// (.github/runbooks/thesis-public-auth.md Step 4). The composition never
// creates the ACS resource and never carries the connection string; an empty
// secret name disables the mailer wiring so the module stays deployable before
// the operator creates the secret. The bearer secret is always required.
var thesisMailerEnabled = !empty(mailerConnectionStringSecretName)
// Workflow wiring is opt-in per secret: the module stays deployable before the
// operator provisions the harness token or the model key, and the runtime
// composes only what it can authenticate (thesis/http/serving.py).
var thesisHarnessEnabled = !empty(harnessBearerTokenSecretName)
var thesisModelKeyEnabled = !empty(modelApiKeySecretName)
var thesisSecrets = concat(
  [
    {
      name: thesisSecretName
      keyVaultUrl: '${keyVaultUri}secrets/${thesisSecretName}'
      identity: thesisIdentityResourceId
    }
  ],
  thesisMailerEnabled ? [
    {
      name: mailerConnectionStringSecretName
      keyVaultUrl: '${keyVaultUri}secrets/${mailerConnectionStringSecretName}'
      identity: thesisIdentityResourceId
    }
  ] : [],
  thesisHarnessEnabled ? [
    {
      name: harnessBearerTokenSecretName
      keyVaultUrl: '${keyVaultUri}secrets/${harnessBearerTokenSecretName}'
      identity: thesisIdentityResourceId
    }
  ] : [],
  thesisModelKeyEnabled ? [
    {
      name: modelApiKeySecretName
      keyVaultUrl: '${keyVaultUri}secrets/${modelApiKeySecretName}'
      identity: thesisIdentityResourceId
    }
  ] : []
)
// The bearer token reaches the container only through a secretRef; no
// provider, model, or broker secret is configured for Thesis. The ACS
// connection string likewise reaches the container only through a secretRef.
var thesisMailerEnvironment = thesisMailerEnabled ? concat([
  {
    name: 'THESIS_API__MAILER_CONNECTION_STRING'
    secretRef: mailerConnectionStringSecretName
  }
], empty(mailerSenderAddress) ? [] : [
  {
    name: 'THESIS_API__MAILER_SENDER_ADDRESS'
    value: mailerSenderAddress
  }
], empty(mailerEndpoint) ? [] : [
  {
    name: 'THESIS_API__MAILER_ENDPOINT'
    value: mailerEndpoint
  }
]) : []
var thesisBaseEnvironment = [
  {
    name: 'AZURE_CLIENT_ID'
    value: thesisIdentityClientId
  }
  // The serve host must bind all interfaces inside the container; the
  // loopback default is only for local use and would fail ingress probes.
  {
    name: 'THESIS_SERVE_HOST'
    value: '0.0.0.0'
  }
  {
    name: 'THESIS_SERVE_PORT'
    value: '${thesisContainerPort}'
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
  // Durable conversation state lives in the account provisioned by
  // thesis-cosmos.bicep. The runtime reaches it with the same managed identity
  // (AZURE_CLIENT_ID above) through Cosmos data-plane RBAC; the account
  // disables local auth, so no key exists in Key Vault or in this module.
  {
    name: 'THESIS_COSMOS_ENDPOINT'
    value: thesisCosmosEndpoint
  }
  {
    name: 'THESIS_COSMOS_DATABASE'
    value: thesisCosmosDatabaseName
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
// The workflow runtime reads these through thesis/http/serving.py: a model
// key plus a reachable harness MCP endpoint compose conversations, control
// tools, and the workflow runtime; either missing leaves the app chat-only.
var thesisWorkflowEnvironment = concat(
  empty(thesisModel) ? [] : [
    {
      name: 'THESIS_MODEL'
      value: thesisModel
    }
  ],
  empty(harnessMcpUrl) ? [] : [
    {
      name: 'HARNESS_MCP_URL'
      value: harnessMcpUrl
    }
  ],
  thesisHarnessEnabled ? [
    {
      name: 'HARNESS_API_BEARER_TOKEN'
      secretRef: harnessBearerTokenSecretName
    }
  ] : [],
  thesisModelKeyEnabled ? [
    {
      name: 'OPENROUTER_API_KEY'
      secretRef: modelApiKeySecretName
    }
  ] : []
)
var thesisApiEnvironment = concat(thesisBaseEnvironment, thesisEntraEnvironment, [
  {
    name: 'THESIS_API__BEARER_TOKEN'
    secretRef: thesisSecretName
  }
], thesisMailerEnvironment, thesisWorkflowEnvironment)

// The vault is referenced, not created: the composition owns the Key Vault.
var keyVaultName = split(replace(keyVaultUri, 'https://', ''), '.')[0]

// Managed certificate names accept lowercase alphanumeric characters and
// hyphens. The derived name replaces dots with hyphens and stays under the
// 63-character resource-name limit.
var thesisCustomCertificateName = take(replace(toLower(customDomainName), '.', '-'), 63)

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

// The managed environment is composed in the same resource group by
// container-apps.bicep; this module receives its resource ID and resolves
// the name for the managed-certificate parent scope.
resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2025-07-01' existing = {
  name: last(split(containerAppsEnvironmentResourceId, '/'))
}

// The free managed certificate is requested only for a non-empty custom
// domain on external ingress. CNAME domain control validation leaves the
// certificate pending until the operator's registrar records resolve; see
// .github/runbooks/domain-hosting.md for the exact DNS records and az
// commands. No certificate file is uploaded and no secret is stored. The
// external-ingress guard means the Static Web App posture (external ingress,
// empty customDomainName) never creates a certificate or a public hostname
// binding on this Container App.
resource thesisManagedCertificate 'Microsoft.App/managedEnvironments/managedCertificates@2025-07-01' = if (enableExternalIngress && !empty(customDomainName)) {
  parent: containerAppsEnvironment
  name: thesisCustomCertificateName
  location: location
  properties: {
    subjectName: customDomainName
    domainControlValidation: 'CNAME'
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
        // A custom hostname binds only to external ingress and references the
        // managed certificate above; the certificateId reference orders the
        // certificate before the app. The Static Web App posture keeps
        // customDomainName empty and ingress internal, so the SWA is the only
        // public hostname owner.
        customDomains: (enableExternalIngress && !empty(customDomainName)) ? [
          {
            name: customDomainName
            bindingType: 'SniEnabled'
            certificateId: thesisManagedCertificate.id
          }
        ] : []
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
  customDomain: customDomainName
}

@description('Identity wiring consumed by the Thesis API, echoed for non-secret composition summaries.')
output thesisIdentity object = {
  resourceId: thesisIdentityResourceId
  clientId: thesisIdentityClientId
}

@description('Non-secret linked-backend target the composition passes to static-web-app.bicep (resourceId and region). The internal Container App keeps ingress internal; the Static Web App is the public origin that proxies /api/* here. fqdn is the Container Apps internal ingress name and is reported for runbook health checks only.')
output backend object = {
  name: thesisApiApp.name
  resourceId: thesisApiApp.id
  region: location
  fqdn: thesisApiApp.properties.configuration.ingress.fqdn
  ingressExternal: enableExternalIngress
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
