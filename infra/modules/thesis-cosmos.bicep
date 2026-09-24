targetScope = 'resourceGroup'

// Thesis durable conversation store (Azure Cosmos DB, SQL API)
// ------------------------------------------------------------
// The Thesis backend keeps every chat, timeline item, execution document, and
// workflow event in Cosmos DB (src/thesis/cosmos/*). Local development uses
// the Cosmos emulator (.github/runbooks/thesis-local-stack.md); this module
// provisions the deployed account so the container app never falls back to
// the emulator defaults that `CosmosSettings.from_environment()` would
// otherwise resolve inside the image.
//
// Isolation and account shape: a dedicated account, separate from any
// market-lake or quant-harness data plane. The account is serverless, so an
// idle Thesis deployment carries no provisioned-RU charge; cost is per-request
// RU plus storage, bounded by the resource-group budget alerts.
//
// Authentication is identity-only. `disableLocalAuth: true` means no account
// key exists to store, leak, or rotate: the runtime authenticates with its
// user-assigned managed identity (AZURE_CLIENT_ID is already injected by
// thesis.bicep) through the Cosmos data-plane contributor role assigned below.
// The runtime's `ensure_database()` remains an idempotent safety net, but the
// database and containers are provisioned here so startup needs only metadata
// reads.
//
// Schema ownership: the container ids and partition keys below are the same
// closed set the runtime composes (`TIMELINE_CONTAINER`/`CATALOG_CONTAINER`/
// `EXECUTIONS_CONTAINER` in src/thesis/cosmos/client.py). Changing a partition
// key here without changing that module would silently break point reads, so
// keep the two in lockstep.
//
// Additive monthly cost envelope (per the cost-controls.bicep style):
// serverless has no fixed hourly charge; storage is billed per GB-month and
// requests per RU. An idle composition costs approximately $0, and the
// per-request cost is covered by the existing resource-group budget alerts.

@description('Globally unique Cosmos DB account name dedicated to Thesis conversation state.')
@minLength(3)
@maxLength(44)
param accountName string

@description('Azure region for the Thesis Cosmos DB account. Serverless accounts are single-region.')
param location string = resourceGroup().location

@description('Tags applied to the Thesis Cosmos DB account and its children.')
param tags object = {}

@description('Object/principal ID of the Thesis runtime user-assigned managed identity that receives the Cosmos data-plane contributor role.')
@minLength(36)
@maxLength(36)
param thesisPrincipalId string

@description('SQL database name. Must match THESIS_COSMOS_DATABASE (default "thesis") so the runtime resolves the same database.')
@minLength(1)
@maxLength(255)
param databaseName string = 'thesis'

// Cosmos DB's built-in data-plane roles are fixed identifiers defined by the
// service: 00000000-0000-0000-0000-000000000001 is Data Reader and
// 00000000-0000-0000-0000-000000000002 is Data Contributor. Contributor is
// the least privilege that lets the runtime read and write items in every
// container; read-only would break chat persistence.
var dataContributorRoleDefinitionId = resourceId(
  'Microsoft.DocumentDB/databaseAccounts/sqlRoleDefinitions',
  accountName,
  '00000000-0000-0000-0000-000000000002'
)

// The container set mirrors src/thesis/cosmos/client.py: one chat-scoped
// timeline, one workspace-scoped catalog, one execution-store document per
// execution. Partition keys are the only read paths the runtime uses.
var containers = [
  {
    id: 'timeline'
    partitionPath: '/chatId'
  }
  {
    id: 'catalog'
    partitionPath: '/workspaceId'
  }
  {
    id: 'executions'
    partitionPath: '/executionId'
  }
]
var containerSummary = [
  for container in containers: {
    id: container.id
    partitionKey: container.partitionPath
  }
]

resource account 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' = {
  name: accountName
  location: location
  tags: tags
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    // Serverless keeps an idle deployment at $0 provisioned throughput.
    capabilities: [
      {
        name: 'EnableServerless'
      }
    ]
    // Identity-only auth: no key material is created, so nothing needs to be
    // stored in Key Vault or rotated. The runtime must present an Entra token.
    disableLocalAuth: true
    // Session consistency matches the emulator default and the durability
    // model: a chat reads its own writes within one session.
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
    // Serverless accounts are single-region; failoverPriority 0 is required.
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
    minimalTlsVersion: 'Tls12'
    // The composition contains no private endpoint module for Cosmos, matching
    // the explicit public-network stance of Key Vault in this deployment
    // (infra/main.bicep allowPublicNetworkAccess); every request stays
    // authenticated by Entra ID and data-plane RBAC.
    publicNetworkAccess: 'Enabled'
  }
}

resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-11-15' = {
  parent: account
  name: databaseName
  properties: {
    resource: {
      id: databaseName
    }
  }
}

resource sqlContainers 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = [
  for container in containers: {
    parent: database
    name: container.id
    properties: {
      resource: {
        id: container.id
        partitionKey: {
          paths: [
            container.partitionPath
          ]
          kind: 'Hash'
        }
      }
    }
  }
]

// Data-plane RBAC is scoped to the whole account so the runtime can reach
// every container with one assignment. Role-assignment names must be GUIDs;
// the deterministic guid() keeps redeployments idempotent.
resource dataContributor 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = {
  parent: account
  name: guid(account.id, thesisPrincipalId, dataContributorRoleDefinitionId)
  properties: {
    roleDefinitionId: dataContributorRoleDefinitionId
    principalId: thesisPrincipalId
    scope: account.id
  }
}

@description('Endpoint the Thesis API consumes as THESIS_COSMOS_ENDPOINT.')
output endpoint string = account.properties.documentEndpoint

@description('Database name the Thesis API consumes as THESIS_COSMOS_DATABASE.')
output databaseName string = database.name

output accountName string = account.name

output accountId string = account.id

output dataContributorRoleAssignmentId string = dataContributor.id

@description('Non-secret persistence summary: account shape, schema, and identity access.')
output persistence object = {
  model: 'serverless SQL API account, container-per-concern schema, identity-only auth'
  endpoint: account.properties.documentEndpoint
  database: database.name
  containers: containerSummary
  account: {
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
    consistency: 'Session'
    multiRegion: false
  }
  runtimeAccess: {
    principalId: thesisPrincipalId
    roleName: 'Cosmos DB Built-in Data Contributor (data plane)'
    roleAssignmentId: dataContributor.id
  }
}
