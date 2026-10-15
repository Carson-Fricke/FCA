targetScope = 'resourceGroup'

@description('Globally unique Azure Container Registry name used as the private runtime image mirror.')
@minLength(5)
@maxLength(50)
param registryName string

@description('Object/principal ID of the runtime user-assigned managed identity.')
@minLength(36)
@maxLength(36)
param runtimePrincipalId string

@description('Harness identity that pulls the combined API and execution images.')
param quantPrincipalId string

@description('Optional deployer principal that imports a private source image and verifies its mirrored digest.')
param imageImportPrincipalId string = ''

@description('Optional Thesis user-assigned identity that pulls the digest-pinned Thesis image. Empty skips the assignment.')
param thesisPrincipalId string = ''

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
@description('Status of the registry-level retention policy. The policy is Premium-SKU-only: the default is disabled because this registry deploys the Basic SKU, and an enabled policy on Basic fails the deployment with SkuNotSupported. Per-service digest pruning is owned by the prune-acr.yml workflow. Set enabled together with a Premium skuName to activate it from Bicep.')
param retentionStatus string = 'disabled'

@allowed([
  'Basic'
  'Standard'
  'Premium'
])
@description('Registry SKU. Basic is the budget default; the registry-level retention policy requires Premium, which is why retentionStatus defaults to disabled.')
param skuName string = 'Basic'

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: registryName
  location: location
  tags: tags
  sku: {
    name: skuName
  }
  properties: {
    adminUserEnabled: false
    dataEndpointEnabled: false
    networkRuleBypassOptions: 'AzureServices'
    policies: {
      // The registry-level retention policy purges only untagged manifests
      // and is enforced only on the Premium SKU. This registry deploys the
      // Basic SKU, so retentionStatus defaults to disabled; enabling it on
      // Basic fails deployment with SkuNotSupported. The mirrored manifests
      // keep their commit-SHA tag either way, so tag-carrying digests are
      // pruned by the per-service workflow (.github/workflows/prune-acr.yml),
      // which keeps the newest two digests plus every digest an active or
      // rollback Container App revision still references.
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

resource quantAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, quantPrincipalId, acrPullRole.id)
  properties: {
    principalId: quantPrincipalId
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

// The Thesis API pulls its own digest-pinned image with its user-assigned
// identity; the lake runtime and deployer assignments do not cover it.
resource thesisAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(thesisPrincipalId)) {
  scope: registry
  name: guid(registry.id, thesisPrincipalId, acrPullRole.id)
  properties: {
    principalId: thesisPrincipalId
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
