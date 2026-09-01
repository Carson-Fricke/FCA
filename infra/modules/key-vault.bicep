targetScope = 'resourceGroup'

@description('Globally unique Key Vault name. This module creates no secrets, keys, or certificates.')
@minLength(3)
@maxLength(24)
param keyVaultName string

@description('Azure region for the Key Vault.')
param location string = resourceGroup().location

@description('Tags applied to the Key Vault.')
param tags object = {}

@description('Explicitly enables the public endpoint when private connectivity is not available. The secure default disables public access; enabling it permits network access but still requires Microsoft Entra authentication and Azure RBAC authorization.')
param allowPublicNetworkAccess bool = false

var keyVaultSecretsOfficerRoleDefinitionId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'
)

resource keyVault 'Microsoft.KeyVault/vaults@2026-02-01' = {
  name: keyVaultName
  location: location
  tags: tags
  properties: {
    accessPolicies: []
    createMode: 'default'
    enabledForDeployment: false
    enabledForDiskEncryption: false
    enabledForTemplateDeployment: false
    enablePurgeProtection: true
    enableRbacAuthorization: true
    enableSoftDelete: true
    networkAcls: {
      bypass: 'None'
      defaultAction: allowPublicNetworkAccess ? 'Allow' : 'Deny'
      ipRules: []
      virtualNetworkRules: []
    }
    publicNetworkAccess: allowPublicNetworkAccess ? 'Enabled' : 'Disabled'
    sku: {
      family: 'A'
      name: 'standard'
    }
    softDeleteRetentionInDays: 90
    tenantId: tenant().tenantId
  }
}

output keyVaultName string = keyVault.name
output keyVaultId string = keyVault.id
output keyVaultUri string = keyVault.properties.vaultUri

@description('Network access posture for later composition. With the secure default, private endpoint and DNS integration must be provided before a runtime workload can reach the vault.')
output networkAccess object = {
  publicNetworkAccess: allowPublicNetworkAccess ? 'Enabled' : 'Disabled'
  privateConnectivityRequired: !allowPublicNetworkAccess
}

@description('Bootstrap metadata for secret seeding. This module intentionally creates no secret values and grants the runtime identity read-only secret access only through the separate access module.')
output secretSeeding object = {
  secretsCreated: false
  requiredRoleName: 'Key Vault Secrets Officer'
  requiredRoleDefinitionId: keyVaultSecretsOfficerRoleDefinitionId
  assignmentScope: keyVault.id
}
