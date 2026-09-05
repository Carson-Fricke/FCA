targetScope = 'resourceGroup'

@description('Name of the user-assigned managed identity attached to runtime workloads.')
@minLength(3)
@maxLength(128)
param runtimeIdentityName string

@description('Name of the separate user-assigned managed identity attached to quant-harness runtime workloads. Keeping it distinct from the lake runtime identity bounds the blast radius of trader credentials and results access.')
@minLength(3)
@maxLength(128)
param quantRuntimeIdentityName string

@description('Name of the separate user-assigned managed identity used by GitHub Actions deployments.')
@minLength(3)
@maxLength(128)
param githubDeployerIdentityName string

@description('Azure region for both user-assigned managed identities.')
param location string = resourceGroup().location

@description('Tags applied to both user-assigned managed identities.')
param tags object = {}

@description('GitHub organization or user that owns the repository. Casing must match the immutable OIDC subject emitted by GitHub.')
@minLength(1)
param githubOwner string

@description('Public numeric GitHub database ID of the repository owner. This is not a credential or secret.')
@minLength(1)
param githubOwnerId string

@description('GitHub repository name without the owner prefix. Casing must match the immutable OIDC subject emitted by GitHub.')
@minLength(1)
param githubRepository string

@description('Public numeric GitHub database ID of the repository. This is not a credential or secret.')
@minLength(1)
param githubRepositoryId string

@description('GitHub Environment name trusted to deploy. Pass the name as shown in GitHub; colons are percent-encoded when constructing the exact OIDC subject. The credential does not trust a branch, tag, pull request, or repository-wide subject.')
@minLength(1)
param githubEnvironment string

var githubOidcIssuer = 'https://token.actions.githubusercontent.com'
var githubOidcAudience = 'api://AzureADTokenExchange'
var encodedGithubEnvironment = replace(githubEnvironment, ':', '%3A')
var githubOidcSubject = 'repo:${githubOwner}@${githubOwnerId}/${githubRepository}@${githubRepositoryId}:environment:${encodedGithubEnvironment}'

resource runtimeIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: runtimeIdentityName
  location: location
  tags: tags
}

resource quantRuntimeIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: quantRuntimeIdentityName
  location: location
  tags: tags
}

resource githubDeployerIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: githubDeployerIdentityName
  location: location
  tags: tags
}

resource githubEnvironmentCredential 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2024-11-30' = {
  name: 'github-deploy'
  parent: githubDeployerIdentity
  properties: {
    audiences: [
      githubOidcAudience
    ]
    issuer: githubOidcIssuer
    subject: githubOidcSubject
  }
}

output runtimeIdentity object = {
  name: runtimeIdentity.name
  resourceId: runtimeIdentity.id
  clientId: runtimeIdentity.properties.clientId
  principalId: runtimeIdentity.properties.principalId
}

output quantRuntimeIdentity object = {
  name: quantRuntimeIdentity.name
  resourceId: quantRuntimeIdentity.id
  clientId: quantRuntimeIdentity.properties.clientId
  principalId: quantRuntimeIdentity.properties.principalId
}

output githubDeployerIdentity object = {
  name: githubDeployerIdentity.name
  resourceId: githubDeployerIdentity.id
  clientId: githubDeployerIdentity.properties.clientId
  principalId: githubDeployerIdentity.properties.principalId
}

@description('Non-secret values required by a later GitHub environment and Azure login step. The workflow must request id-token: write, target the exact environment named here, and emit the immutable subject.')
output githubOidc object = {
  audience: githubOidcAudience
  issuer: githubOidcIssuer
  subject: githubEnvironmentCredential.properties.subject
  environment: githubEnvironment
  azureClientId: githubDeployerIdentity.properties.clientId
  azureTenantId: tenant().tenantId
  azureSubscriptionId: subscription().subscriptionId
  requiredGithubSubjectMode: 'Immutable default subject; custom subject templates must be disabled'
}
