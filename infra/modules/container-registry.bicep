targetScope = 'resourceGroup'

@description('Globally unique Azure Container Registry name used as the private runtime image mirror.')
@minLength(5)
@maxLength(50)
param registryName string

@description('Object/principal ID of the runtime user-assigned managed identity.')
@minLength(36)
@maxLength(36)
param runtimePrincipalId string

@description('Optional deployer principal that imports a private source image and verifies its mirrored digest.')
param imageImportPrincipalId string = ''

@description('Azure region for the private runtime registry.')
param location string = resourceGroup().location

@description('Tags applied to the private runtime registry.')
param tags object = {}

@description('Days the registry-level retention policy keeps an untagged manifest before automated cleanup. The Azure rollback window is 7 days, which is also the ACR minimum; the supported range is 7-90 days.')
@minValue(7)
@maxValue(90)
param retentionDays int = 7

@allowed([
  'enabled'
  'disabled'
])
@description('Status of the registry-level retention policy. Enabled by default so the policy applies from Bicep with no portal change; disable it only to hand deletion fully to the per-service prune workflow.')
param retentionStatus string = 'enabled'

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: registryName
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
    dataEndpointEnabled: false
    networkRuleBypassOptions: 'AzureServices'
    policies: {
      // The registry-level retention policy purges only untagged manifests
      // after the configured age and is enforced only on the Premium SKU. The
      // mirrored manifests keep their commit-SHA tag, so tag-carrying digests
      // need the per-service prune workflow (.github/workflows/prune-acr.yml),
      // which keeps the newest two digests plus every digest an active or
      // rollback Container App revision still references. Upgrading the SKU
      // activates this policy for untagged manifests with no further change.
      retentionPolicy: {
        days: retentionDays
        status: retentionStatus
      }
    }
    publicNetworkAccess: 'Enabled'
    zoneRedundancy: 'Disabled'
  }
}

resource acrPullRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' existing = {
  scope: subscription()
  name: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
}

resource runtimeAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, runtimePrincipalId, acrPullRole.id)
  properties: {
    principalId: runtimePrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: acrPullRole.id
  }
}

resource imageImportAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(imageImportPrincipalId)) {
  scope: registry
  name: guid(registry.id, imageImportPrincipalId, acrPullRole.id)
  properties: {
    principalId: imageImportPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: acrPullRole.id
  }
}

output registry object = {
  name: registry.name
  resourceId: registry.id
  loginServer: registry.properties.loginServer
  adminUserEnabled: registry.properties.adminUserEnabled
  anonymousPullEnabled: false
  retentionDays: retentionDays
  retentionStatus: retentionStatus
  runtimeAcrPullAssignmentId: runtimeAcrPull.id
  imageImportAcrPullAssignmentId: empty(imageImportPrincipalId) ? null : imageImportAcrPull.id
}
