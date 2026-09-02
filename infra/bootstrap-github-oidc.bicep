targetScope = 'resourceGroup'

@description('Azure region for the GitHub deployer and runtime user-assigned identities.')
param location string = resourceGroup().location

@description('Short project prefix used by the recurring market-lake deployment.')
@minLength(2)
@maxLength(16)
param resourceNamePrefix string = 'fca-ml'

@description('Deployment environment trusted by the GitHub federated credential.')
@minLength(1)
@maxLength(12)
param environmentName string = 'dev'

@description('GitHub organization or user owning the repository. Casing must match the immutable OIDC subject.')
@minLength(1)
param githubOwner string

@description('Public numeric GitHub database ID of the repository owner.')
@minLength(1)
param githubOwnerId string

@description('GitHub repository name without the owner prefix. Casing must match the immutable OIDC subject.')
@minLength(1)
param githubRepository string

@description('Public numeric GitHub database ID of the repository.')
@minLength(1)
param githubRepositoryId string

var namePrefix = '${toLower(resourceNamePrefix)}-${toLower(environmentName)}'
var runtimeIdentityName = '${namePrefix}-runtime'
var githubDeployerIdentityName = '${namePrefix}-github-deployer'
var containerRegistryName = 'fca${take(uniqueString(resourceGroup().id, namePrefix, 'registry'), 18)}'
var tags = {
  application: 'fca'
  product: 'market-lake'
  environment: environmentName
  managedBy: 'bicep'
}

module identities './modules/identities.bicep' = {
  name: 'github-oidc-bootstrap-identities'
  params: {
    runtimeIdentityName: runtimeIdentityName
    githubDeployerIdentityName: githubDeployerIdentityName
    location: location
    tags: tags
    githubOwner: githubOwner
    githubOwnerId: githubOwnerId
    githubRepository: githubRepository
    githubRepositoryId: githubRepositoryId
    githubEnvironment: environmentName
  }
}

module containerRegistry './modules/container-registry.bicep' = {
  name: 'github-oidc-bootstrap-registry'
  params: {
    registryName: containerRegistryName
    runtimePrincipalId: identities.outputs.runtimeIdentity.principalId
    imageImportPrincipalId: identities.outputs.githubDeployerIdentity.principalId
    location: location
    tags: tags
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

var githubDeployerRbacCondition = '((!(ActionMatches{\'Microsoft.Authorization/roleAssignments/write\'})) OR (@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {7f951dda-4ed3-4680-a7ca-43fe172d538d, ba92f5b4-2d11-453d-a403-e96b0029c9fe, 974c5e8b-45b9-4653-ba55-5f855dd0fb88, 0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3, 4633458b-17de-408a-b874-0445c86b69e6} AND @Request[Microsoft.Authorization/roleAssignments:PrincipalId] ForAnyOfAnyValues:GuidEquals {${identities.outputs.runtimeIdentity.principalId}})) AND ((!(ActionMatches{\'Microsoft.Authorization/roleAssignments/delete\'})) OR (@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {7f951dda-4ed3-4680-a7ca-43fe172d538d, ba92f5b4-2d11-453d-a403-e96b0029c9fe, 974c5e8b-45b9-4653-ba55-5f855dd0fb88, 0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3, 4633458b-17de-408a-b874-0445c86b69e6} AND @Resource[Microsoft.Authorization/roleAssignments:PrincipalId] ForAnyOfAnyValues:GuidEquals {${identities.outputs.runtimeIdentity.principalId}}))'

resource githubDeployerContributorAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, githubDeployerIdentityName, contributorRole.id)
  properties: {
    principalId: identities.outputs.githubDeployerIdentity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: contributorRole.id
  }
}

resource githubDeployerRbacAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, githubDeployerIdentityName, roleBasedAccessControlAdministratorRole.id)
  properties: {
    condition: githubDeployerRbacCondition
    conditionVersion: '2.0'
    description: 'Permit the GitHub deployer to manage only the market-lake runtime identity data-role assignments.'
    principalId: identities.outputs.githubDeployerIdentity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: roleBasedAccessControlAdministratorRole.id
  }
}

@description('Non-secret values used to configure the GitHub Environment after this one-time deployment.')
output githubConfiguration object = {
  environment: environmentName
  azureClientId: identities.outputs.githubDeployerIdentity.clientId
  azureTenantId: tenant().tenantId
  azureSubscriptionId: subscription().subscriptionId
  azureResourceGroup: resourceGroup().name
  azureLocation: location
  resourceNamePrefix: resourceNamePrefix
  azureContainerRegistry: containerRegistry.outputs.registry
  oidc: identities.outputs.githubOidc
}

@description('Least-privilege recurring deployment assignments created by this bootstrap.')
output deploymentAccess object = {
  contributor: {
    assignmentId: githubDeployerContributorAccess.id
    scope: resourceGroup().id
  }
  runtimeRoleAdministrator: {
    assignmentId: githubDeployerRbacAccess.id
    scope: resourceGroup().id
    condition: githubDeployerRbacCondition
  }
}
