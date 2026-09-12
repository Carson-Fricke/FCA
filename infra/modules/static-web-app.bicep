targetScope = 'resourceGroup'

// Thesis public web host
// ----------------------
// The Thesis SvelteKit SPA is served from Azure Static Web Apps on the
// Standard tier. Standard is required for the linked backend that proxies
// /api/* to the internal Thesis Container App; the browser then sees one
// public origin while the Container App keeps internal ingress
// (adr-001-invite-only-public-multi-user-access-for-thesis.md).
//
// This module provisions only the empty host. The linked backend and the
// optional custom-domain hostname binding, managed certificate, and DNS
// validation are owned by a later task (T301); they are intentionally not
// created here so this module stays deployable on its own. T301 consumes the
// resourceId and defaultHostname outputs below.
//
// The deployment token is deliberately not a deployment output: secrets must
// not appear in deployment outputs or Git. CI reads it at deploy time with
// `az staticwebapp secrets list --name <name>` under the deployer identity.

@description('Globally unique name of the Azure Static Web Apps resource that hosts the Thesis SvelteKit SPA.')
@minLength(2)
@maxLength(60)
param staticWebAppName string

@description('Azure region for the Static Web App. Standard tier hosts are available in a subset of regions, so accept the deployment region or an operator override.')
param location string = resourceGroup().location

@description('Tags applied to the Static Web App.')
param tags object = {}

@description('Optional operator-owned custom hostname for the public Thesis site, for example thesis.<operator-domain>. Empty keeps the generated azurestaticapps.net hostname. The hostname binding, managed certificate, and registrar-side DNS validation are owned by T301; this module only carries the requested value through to its output.')
param customDomainName string = ''

// Static Web Apps Standard is the minimum tier with linked backends, custom
// domains, and managed certificates; Free has none of those.
var staticWebAppSku = 'Standard'

resource thesisStaticWebApp 'Microsoft.Web/staticSites@2023-12-01' = {
  name: staticWebAppName
  location: location
  tags: tags
  sku: {
    name: staticWebAppSku
    tier: staticWebAppSku
  }
  // No properties are required to provision an empty host. The SPA is
  // published by the deployment workflow through the deployment token; no
  // repository branch is linked here.
  properties: {}
}

@description('Public hosting surface of the Thesis SPA. defaultHostname is the generated azurestaticapps.net origin until T301 binds customDomainName.')
output staticWebApp object = {
  name: thesisStaticWebApp.name
  resourceId: thesisStaticWebApp.id
  defaultHostname: thesisStaticWebApp.properties.defaultHostname
  customDomain: customDomainName
}