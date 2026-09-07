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
  runtimeAcrPullAssignmentId: runtimeAcrPull.id
  imageImportAcrPullAssignmentId: empty(imageImportPrincipalId) ? null : imageImportAcrPull.id
}
