targetScope = 'resourceGroup'

// Thesis workspace isolation model
// --------------------------------
// Every Thesis workspace is an independently owned research entity, so its
// durable content must never share a data-plane scope with market-lake data
// or quant-harness results. This module provisions a dedicated Standard_LRS
// storage account that is separate from the market-lake lake account.
//
// Container-per-workspace: each registered workspace receives its own blob
// container named '<prefix><workspace-id>' (prefix 'thesis-ws-'). Containers
// are the strongest native blob boundary short of separate accounts: a
// workspace can be scoped, audited, deleted, and quota-reported without
// touching sibling workspaces, and no workspace's blobs share a prefix
// namespace with another workspace. Git repositories are hosted separately by
// Gitea. repository/ manifests and catalog/repository.json are catalog metadata,
// not authoritative Git object storage; artifacts/ and scratch/ remain isolated
// operational namespaces.
//
// Workspace registry: the 'workspaces' table on this account records one
// entity per workspace following the AzureWorkspaceEntity contract in
// src/thesis/workspaces/azure_contract.py (PartitionKey 'workspaces|<owner>',
// RowKey '<workspace-id>', ExecutionScopeId, PersistenceScopeId). The
// persistence scope is the workspace container's ARM resource ID; the
// execution scope is the Thesis API Container App resource ID. The runtime
// must validate both scopes before touching workspace content. Bicep cannot
// author Table entities, so this module provisions the table and containers
// and the deployment runbook seeds one registry row per provisioned
// workspace.
//
// Registry parameter: the workspaceRegistry parameter seeds the initial
// workspaces (workspaceId plus ownerId). Runtime-created workspaces must
// reuse the exported container prefix, keep one container per workspace, and
// write the matching registry entity before first use. No workspace content
// may be written outside its registered persistence scope.
//
// No tiering policy: unlike the lake account, workspace repositories are
// deliberately not moved to cooler tiers by lifecycle rules because
// Git-backed completeness is a chosen product property
// (.github/architecture/fca-thesis.md); changing that requires an explicit
// product decision.
//
// Additive monthly cost envelope (per the cost-controls.bicep style): the
// account has no fixed charge; Standard_LRS hot blobs cost about $0.02 per
// GB-month, and the registry table plus per-workspace containers stay empty
// until used, so idle persistence is effectively $0. Workspace limits in
// config/thesis/workspace.yaml (max_repository_bytes, max_artifact_bytes)
// bound per-workspace growth at the application layer.

@description('Globally unique Storage account name dedicated to Thesis workspace persistence.')
@minLength(3)
@maxLength(24)
param storageAccountName string

@description('Azure region for the Thesis workspace Storage account.')
param location string = resourceGroup().location

@description('Tags applied to the Thesis workspace Storage account and its children.')
param tags object = {}

@description('Object/principal ID of the Thesis runtime user-assigned managed identity that receives least-privilege data roles on this account.')
@minLength(36)
@maxLength(36)
param thesisPrincipalId string

@description('Initial workspace registry seeded with one blob container per entry. Each entry requires workspaceId and ownerId; Table entity rows are seeded by the deployment runbook.')
param workspaceRegistry array = []

var workspaceContainerPrefix = 'thesis-ws-'
var workspaceRegistryTableName = 'workspaces'
// Blob container names allow lowercase letters, numbers, and hyphens, so
// workspace identifiers are normalized with underscores mapped to hyphens.
var workspaceContainerNames = [for entry in workspaceRegistry: '${workspaceContainerPrefix}${toLower(replace(entry.workspaceId, '_', '-'))}']
var provisionedWorkspaces = [for (entry, index) in workspaceRegistry: {
  workspaceId: entry.workspaceId
  ownerId: entry.ownerId
  containerName: workspaceContainerNames[index]
  persistenceScopeId: '${storageAccount.id}/blobServices/default/containers/${workspaceContainerNames[index]}'
}]

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowCrossTenantReplication: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    // A flat blob namespace is sufficient for workspace repositories and
    // avoids the hierarchical namespace cost premium; isolation comes from
    // the container-per-workspace layout above, not from ADLS ACLs.
    isHnsEnabled: false
    isLocalUserEnabled: false
    isNfsV3Enabled: false
    isSftpEnabled: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  name: 'default'
  parent: storageAccount
  properties: {
    isVersioningEnabled: false
  }
}

resource workspaceContainers 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = [for containerName in workspaceContainerNames: {
  name: containerName
  parent: blobService
  properties: {
    publicAccess: 'None'
  }
}]

resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2023-05-01' = {
  name: 'default'
  parent: storageAccount
  properties: {}
}

resource workspaceRegistryTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = {
  name: workspaceRegistryTableName
  parent: tableService
  properties: {
    signedIdentifiers: []
  }
}

resource storageBlobDataContributorRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' existing = {
  scope: subscription()
  name: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
}

resource storageTableDataContributorRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' existing = {
  scope: subscription()
  name: '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
}

resource thesisBlobAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storageAccount
  name: guid(storageAccount.id, thesisPrincipalId, storageBlobDataContributorRole.id)
  properties: {
    principalId: thesisPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: storageBlobDataContributorRole.id
  }
}

resource thesisTableAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storageAccount
  name: guid(storageAccount.id, thesisPrincipalId, storageTableDataContributorRole.id)
  properties: {
    principalId: thesisPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: storageTableDataContributorRole.id
  }
}

output storageAccountName string = storageAccount.name

output storageAccountId string = storageAccount.id

output registryTableName string = workspaceRegistryTableName

@description('Container prefix every workspace container must use so runtime-created workspaces cannot diverge from the provisioned layout.')
output containerPrefix string = workspaceContainerPrefix

@description('Non-secret isolation summary: one container per registered workspace with its persistence scope, plus the registry table binding.')
output isolation object = {
  model: 'container-per-workspace on a dedicated account with a registry table'
  registryTable: workspaceRegistryTable.name
  containerPrefix: workspaceContainerPrefix
  provisioned: provisionedWorkspaces
  account: {
    allowSharedKeyAccess: false
    publicBlobAccess: false
    hierarchicalNamespace: false
    tieringPolicy: 'none (Git-backed completeness is a chosen product property)'
  }
  runtimeAccess: {
    roleName: 'Storage Blob Data Contributor and Storage Table Data Contributor'
    roleAssignmentIds: [
      thesisBlobAccess.id
      thesisTableAccess.id
    ]
  }
}
