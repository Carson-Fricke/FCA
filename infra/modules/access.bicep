targetScope = 'resourceGroup'

@description('Allow the deployer to read only the secure Gitea database deployment input.')
param enableGitea bool = false

@description('Object/principal ID of the runtime user-assigned managed identity.')
@minLength(36)
@maxLength(36)
param runtimePrincipalId string

@description('Identity the dispatcher may attach to ingestion groups.')
param runtimeIdentityResourceId string

@description('Object/principal ID of the quant-harness runtime user-assigned managed identity.')
@minLength(36)
@maxLength(36)
param quantPrincipalId string

@description('Object/principal ID of the GitHub deployer user-assigned managed identity.')
@minLength(36)
@maxLength(36)
param githubDeployerPrincipalId string

@description('Name of the GitHub deployer user-assigned managed identity. Matches the standalone bootstrap so the conditioned Role Based Access Control Administrator assignment resolves to the same resource.')
@minLength(1)
param githubDeployerIdentityName string

@description('Full resource ID of the existing storage account in this module deployment resource group. Runtime data roles are scoped only to this account; cross-resource-group IDs are not supported by this resource-group module.')
@minLength(1)
param storageAccountResourceId string

@description('Full resource ID of the existing Key Vault in this module deployment resource group. Runtime secret access is scoped only to this vault; cross-resource-group IDs are not supported by this resource-group module.')
@minLength(1)
param keyVaultResourceId string

@description('Whether an authorized caller should perform the one-time GitHub deployer bootstrap. True creates Contributor at this resource group plus a conditioned Role Based Access Control Administrator assignment at the resource-group scope. Use false for recurring GitHub deployments because the conditioned deployer cannot create or update its own administrator assignments.')
param assignGithubDeployerRoles bool = false

var storageAccountName = last(split(storageAccountResourceId, '/'))
var keyVaultName = last(split(keyVaultResourceId, '/'))

resource storageAccount 'Microsoft.Storage/storageAccounts@2025-06-01' existing = {
  name: storageAccountName
}

resource keyVault 'Microsoft.KeyVault/vaults@2026-02-01' existing = {
  name: keyVaultName
}

resource storageBlobDataContributorRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' existing = {
  scope: subscription()
  name: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
}

resource storageQueueDataContributorRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' existing = {
  scope: subscription()
  name: '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
}

resource storageTableDataContributorRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' existing = {
  scope: subscription()
  name: '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
}

resource keyVaultSecretsUserRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' existing = {
  scope: subscription()
  name: '4633458b-17de-408a-b874-0445c86b69e6'
}

resource giteaDatabasePassword 'Microsoft.KeyVault/vaults/secrets@2026-02-01' existing = {
  parent: keyVault
  name: 'thesis-gitea-database-password'
}

resource giteaDatabasePasswordReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (enableGitea) {
  name: guid(giteaDatabasePassword.id, githubDeployerPrincipalId, keyVaultSecretsUserRole.id)
  scope: giteaDatabasePassword
  properties: {
    principalId: githubDeployerPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: keyVaultSecretsUserRole.id
  }
}

resource contributorRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' existing = {
  scope: subscription()
  name: 'b24988ac-6180-42a0-ab88-20f7382dd24c'
}

resource roleBasedAccessControlAdministratorRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' existing = {
  scope: subscription()
  name: 'f58310d9-a9f6-439a-9e8d-f62e7b41a168'
}

// The condition bounds role-assignment writes/deletes to the seven allow-listed
// data/control-plane role definitions. The principal is intentionally NOT restricted:
// the deployment creates workload identities (the quant-harness and Thesis
// user-assigned identities) whose principal IDs are unknown at bootstrap time,
// and those identities legitimately receive these data roles.
var githubDeployerRbacCondition = '((!(ActionMatches{\'Microsoft.Authorization/roleAssignments/write\'})) OR (@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {7f951dda-4ed3-4680-a7ca-43fe172d538d, ba92f5b4-2d11-453d-a403-e96b0029c9fe, 974c5e8b-45b9-4653-ba55-5f855dd0fb88, 0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3, 4633458b-17de-408a-b874-0445c86b69e6, 5d977122-f97e-4b4d-a52f-6b43003ddb4d, f1a07417-d97a-45cb-824c-7a7467783830})) AND ((!(ActionMatches{\'Microsoft.Authorization/roleAssignments/delete\'})) OR (@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {7f951dda-4ed3-4680-a7ca-43fe172d538d, ba92f5b4-2d11-453d-a403-e96b0029c9fe, 974c5e8b-45b9-4653-ba55-5f855dd0fb88, 0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3, 4633458b-17de-408a-b874-0445c86b69e6, 5d977122-f97e-4b4d-a52f-6b43003ddb4d, f1a07417-d97a-45cb-824c-7a7467783830}))'

resource runtimeBlobAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storageAccount
  name: guid(storageAccount.id, runtimePrincipalId, storageBlobDataContributorRole.id)
  properties: {
    principalId: runtimePrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: storageBlobDataContributorRole.id
  }
}

resource runtimeQueueAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storageAccount
  name: guid(storageAccount.id, runtimePrincipalId, storageQueueDataContributorRole.id)
  properties: {
    principalId: runtimePrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: storageQueueDataContributorRole.id
  }
}

resource runtimeTableAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storageAccount
  name: guid(storageAccount.id, runtimePrincipalId, storageTableDataContributorRole.id)
  properties: {
    principalId: runtimePrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: storageTableDataContributorRole.id
  }
}

resource runtimeSecretAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: keyVault
  name: guid(keyVault.id, runtimePrincipalId, keyVaultSecretsUserRole.id)
  properties: {
    principalId: runtimePrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: keyVaultSecretsUserRole.id
  }
}

resource quantBlobAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storageAccount
  name: guid(storageAccount.id, quantPrincipalId, storageBlobDataContributorRole.id)
  properties: {
    principalId: quantPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: storageBlobDataContributorRole.id
  }
}

resource quantTableAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storageAccount
  name: guid(storageAccount.id, quantPrincipalId, storageTableDataContributorRole.id)
  properties: {
    principalId: quantPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: storageTableDataContributorRole.id
  }
}

resource quantSecretAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: keyVault
  name: guid(keyVault.id, quantPrincipalId, keyVaultSecretsUserRole.id)
  properties: {
    principalId: quantPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: keyVaultSecretsUserRole.id
  }
}

resource githubDeployerContributorAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (assignGithubDeployerRoles) {
  name: guid(resourceGroup().id, githubDeployerPrincipalId, contributorRole.id)
  properties: {
    principalId: githubDeployerPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: contributorRole.id
  }
}

// The recurring rebootstrap grants a single resource-group-scoped, conditioned
// Role Based Access Control Administrator. The scope is the resource group, not
// one account, so the grant also covers resources the deployment creates later
// (for example the dedicated Thesis workspace storage account). The resource
// name matches the standalone bootstrap so a rebootstrap updates that exact
// assignment rather than adding a second, divergent one.
resource githubDeployerRbacAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (assignGithubDeployerRoles) {
  name: guid(resourceGroup().id, githubDeployerIdentityName, roleBasedAccessControlAdministratorRole.id)
  properties: {
    condition: githubDeployerRbacCondition
    conditionVersion: '2.0'
    description: 'Permit the GitHub deployer to manage only the allow-listed data-role assignments (AcrPull, Storage Blob/Queue/Table, and Key Vault Secrets User) for any workload principal in this resource group.'
    principalId: githubDeployerPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: roleBasedAccessControlAdministratorRole.id
  }
}

output runtimeRoleAssignments object = {
  storageAccountId: storageAccount.id
  keyVaultId: keyVault.id
  assignments: [
    {
      roleName: 'Storage Blob Data Contributor'
      roleAssignmentId: runtimeBlobAccess.id
    }
    {
      roleName: 'Storage Queue Data Contributor'
      roleAssignmentId: runtimeQueueAccess.id
    }
    {
      roleName: 'Storage Table Data Contributor'
      roleAssignmentId: runtimeTableAccess.id
    }
    {
      roleName: 'Key Vault Secrets User'
      roleAssignmentId: runtimeSecretAccess.id
    }
  ]
}

@description('Least-privilege data roles for the quant-harness runtime identity. Blob covers results and state artifacts, tables cover the kill-switch and run-catalog tables, and Key Vault Secrets User covers trader credential reads. Queue access is intentionally not granted.')
output quantRoleAssignments object = {
  storageAccountId: storageAccount.id
  keyVaultId: keyVault.id
  assignments: [
    {
      roleName: 'Storage Blob Data Contributor'
      roleAssignmentId: quantBlobAccess.id
    }
    {
      roleName: 'Storage Table Data Contributor'
      roleAssignmentId: quantTableAccess.id
    }
    {
      roleName: 'Key Vault Secrets User'
      roleAssignmentId: quantSecretAccess.id
    }
  ]
}

@description('Deployer bootstrap state. An authorized caller must deploy once with assignments enabled, then recurring GitHub deployments must use false. Contributor manages resource-group resources; the conditioned RBAC Administrator can assign only the required data and ingestion-control roles to any workload principal at the resource-group scope (covering accounts and vaults created by later deployments).')
output githubDeployerBootstrap object = {
  assignmentsEnabled: assignGithubDeployerRoles
  roles: [
    {
      name: 'Contributor'
      roleDefinitionId: contributorRole.id
      scope: resourceGroup().id
    }
    {
      name: 'Role Based Access Control Administrator'
      roleDefinitionId: roleBasedAccessControlAdministratorRole.id
      scope: resourceGroup().id
      condition: githubDeployerRbacCondition
    }
  ]
  bootstrapCallerRequires: 'Microsoft.Authorization/roleAssignments/write at the resource-group scope'
  recurringGithubValue: false
  rebootstrapWhen: 'A wave adds a new role-assignment scope (such as a new storage account) or a new workload principal, or the allow-listed data roles change'
}

resource ingestionIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' existing = {
  name: last(split(runtimeIdentityResourceId, '/'))
}
resource aciDispatcherAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, quantPrincipalId, '5d977122-f97e-4b4d-a52f-6b43003ddb4d')
  properties: {
    principalId: quantPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5d977122-f97e-4b4d-a52f-6b43003ddb4d')
  }
}
resource ingestionIdentityAttachment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(ingestionIdentity.id, quantPrincipalId, 'f1a07417-d97a-45cb-824c-7a7467783830')
  scope: ingestionIdentity
  properties: {
    principalId: quantPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'f1a07417-d97a-45cb-824c-7a7467783830')
  }
}
