targetScope = 'resourceGroup'

@description('Object/principal ID of the runtime user-assigned managed identity.')
@minLength(36)
@maxLength(36)
param runtimePrincipalId string

@description('Object/principal ID of the GitHub deployer user-assigned managed identity.')
@minLength(36)
@maxLength(36)
param githubDeployerPrincipalId string

@description('Full resource ID of the existing storage account in this module deployment resource group. Runtime data roles are scoped only to this account; cross-resource-group IDs are not supported by this resource-group module.')
@minLength(1)
param storageAccountResourceId string

@description('Full resource ID of the existing Key Vault in this module deployment resource group. Runtime secret access is scoped only to this vault; cross-resource-group IDs are not supported by this resource-group module.')
@minLength(1)
param keyVaultResourceId string

@description('Whether an authorized caller should perform the one-time GitHub deployer bootstrap. True creates Contributor at this resource group plus conditioned Role Based Access Control Administrator assignments on the storage account and Key Vault. Use false for recurring GitHub deployments because the conditioned deployer cannot create or update its own administrator assignments.')
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

resource contributorRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' existing = {
  scope: subscription()
  name: 'b24988ac-6180-42a0-ab88-20f7382dd24c'
}

resource roleBasedAccessControlAdministratorRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' existing = {
  scope: subscription()
  name: 'f58310d9-a9f6-439a-9e8d-f62e7b41a168'
}

var githubDeployerStorageRbacCondition = '((!(ActionMatches{\'Microsoft.Authorization/roleAssignments/write\'})) OR (@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {ba92f5b4-2d11-453d-a403-e96b0029c9fe, 974c5e8b-45b9-4653-ba55-5f855dd0fb88, 0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3} AND @Request[Microsoft.Authorization/roleAssignments:PrincipalId] ForAnyOfAnyValues:GuidEquals {${runtimePrincipalId}})) AND ((!(ActionMatches{\'Microsoft.Authorization/roleAssignments/delete\'})) OR (@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {ba92f5b4-2d11-453d-a403-e96b0029c9fe, 974c5e8b-45b9-4653-ba55-5f855dd0fb88, 0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3} AND @Resource[Microsoft.Authorization/roleAssignments:PrincipalId] ForAnyOfAnyValues:GuidEquals {${runtimePrincipalId}}))'
var githubDeployerKeyVaultRbacCondition = '((!(ActionMatches{\'Microsoft.Authorization/roleAssignments/write\'})) OR (@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {4633458b-17de-408a-b874-0445c86b69e6} AND @Request[Microsoft.Authorization/roleAssignments:PrincipalId] ForAnyOfAnyValues:GuidEquals {${runtimePrincipalId}})) AND ((!(ActionMatches{\'Microsoft.Authorization/roleAssignments/delete\'})) OR (@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {4633458b-17de-408a-b874-0445c86b69e6} AND @Resource[Microsoft.Authorization/roleAssignments:PrincipalId] ForAnyOfAnyValues:GuidEquals {${runtimePrincipalId}}))'

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

resource githubDeployerContributorAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (assignGithubDeployerRoles) {
  name: guid(resourceGroup().id, githubDeployerPrincipalId, contributorRole.id)
  properties: {
    principalId: githubDeployerPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: contributorRole.id
  }
}

resource githubDeployerStorageRbacAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (assignGithubDeployerRoles) {
  scope: storageAccount
  name: guid(storageAccount.id, githubDeployerPrincipalId, roleBasedAccessControlAdministratorRole.id)
  properties: {
    condition: githubDeployerStorageRbacCondition
    conditionVersion: '2.0'
    principalId: githubDeployerPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: roleBasedAccessControlAdministratorRole.id
  }
}

resource githubDeployerKeyVaultRbacAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (assignGithubDeployerRoles) {
  scope: keyVault
  name: guid(keyVault.id, githubDeployerPrincipalId, roleBasedAccessControlAdministratorRole.id)
  properties: {
    condition: githubDeployerKeyVaultRbacCondition
    conditionVersion: '2.0'
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

@description('Deployer bootstrap state. An authorized caller must deploy once with assignments enabled, then recurring GitHub deployments must use false. Contributor manages resource-group resources; conditioned RBAC Administrator can assign only the required data roles to the runtime principal at the exact storage account and Key Vault scopes.')
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
      scope: storageAccount.id
      condition: githubDeployerStorageRbacCondition
    }
    {
      name: 'Role Based Access Control Administrator'
      roleDefinitionId: roleBasedAccessControlAdministratorRole.id
      scope: keyVault.id
      condition: githubDeployerKeyVaultRbacCondition
    }
  ]
  bootstrapCallerRequires: 'Microsoft.Authorization/roleAssignments/write at the resource-group scope'
  recurringGithubValue: false
  rebootstrapWhen: 'The runtime principal ID or the set of roles assigned by this module changes'
}
