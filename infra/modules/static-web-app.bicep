targetScope = 'resourceGroup'

// Thesis public web host
// ----------------------
// The Thesis SvelteKit SPA is served from Azure Static Web Apps on the
// Standard tier. Standard is required for the linked backend that proxies
// /api/* to the internal Thesis Container App and for the custom-domain
// binding with its free Azure-managed certificate; the browser then sees one
// public origin while the Container App keeps internal ingress
// (adr-001-invite-only-public-multi-user-access-for-thesis.md).
//
// T301 owns the linked backend and the custom-domain binding. The linked
// backend is opt-in: the composition passes the Thesis API resource id and
// region from thesis.bicep output `backend`, and this module links it as the
// backend of this host. An empty thesisApiResourceId keeps the module
// deployable on its own with no backend, so a Thesis-disabled composition is
// unchanged.
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

@description('Optional operator-owned custom hostname for the public Thesis site, for example thesis.<operator-domain>. Empty keeps the generated azurestaticapps.net hostname. A non-empty value binds the hostname to this Static Web App with an Azure-managed certificate; the registrar-side CNAME and _dnsauth TXT records stay with the operator (see .github/runbooks/domain-hosting.md).')
param customDomainName string = ''

@description('Full resource ID of the internal Thesis API Container App to link as the Static Web App backend. Empty leaves the host with no linked backend so the module stays deployable before the composition wires it. The composition passes thesis.bicep output backend.resourceId.')
param thesisApiResourceId string = ''

@description('Azure region of the Thesis API Container App, used as the linked-backend region. Defaults to the Static Web App region; the composition passes thesis.bicep output backend.region.')
param thesisApiRegion string = location

// Static Web Apps Standard is the minimum tier with linked backends, custom
// domains, and managed certificates; Free has none of those.
var staticWebAppSku = 'Standard'

// SWA linked backends are keyed by the backend resource name. The Azure CLI
// derives the same name from the backend resource id (link_backend ->
// parse_resource_id(...)['name']), so a composed deployment and an operator
// `az staticwebapp backends link` converge on the same linked backend name.
var linkedBackendName = !empty(thesisApiResourceId) ? last(split(thesisApiResourceId, '/')) : ''

// Static Web Apps validates a subdomain with a CNAME delegation or a
// `_dnsauth.<subdomain>` TXT token. The runbook uses the TXT token, so pin
// that here instead of relying on the CLI default (cname-delegation).
var customDomainValidationMethod = 'dns-txt-token'

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

// Linked backend ("bring your own API"): the SPA calls the API on the same
// origin under /api/* and the host proxies to the internal Container App. The
// backend resource id and region are non-secret identifiers; no host header,
// token, or secret is configured here. See .github/runbooks/domain-hosting.md
// for the internal-ingress reachability caveat.
resource thesisApiLinkedBackend 'Microsoft.Web/staticSites/linkedBackends@2023-12-01' = if (!empty(thesisApiResourceId)) {
  parent: thesisStaticWebApp
  name: linkedBackendName
  properties: {
    backendResourceId: thesisApiResourceId
    region: thesisApiRegion
  }
}

// Custom-domain binding with the free Azure-managed certificate. Azure issues
// and renews the certificate after the operator creates the registrar records;
// no certificate file or secret is stored. Azure reports the validation token
// for the TXT record, which the runbook reads with
// `az staticwebapp hostname show --hostname <name> --query validationToken`.
resource thesisCustomDomainBinding 'Microsoft.Web/staticSites/customDomains@2023-12-01' = if (!empty(customDomainName)) {
  parent: thesisStaticWebApp
  name: customDomainName
  properties: {
    validationMethod: customDomainValidationMethod
  }
}

@description('Public hosting surface of the Thesis SPA. defaultHostname is the generated azurestaticapps.net origin and the CNAME target for a custom domain.')
output staticWebApp object = {
  name: thesisStaticWebApp.name
  resourceId: thesisStaticWebApp.id
  defaultHostname: thesisStaticWebApp.properties.defaultHostname
  customDomain: customDomainName
}

@description('Linked-backend wiring for the internal Thesis API. enabled is false when the composition does not pass thesisApiResourceId; the backend host must be reachable from the Static Web App per the domain-hosting runbook caveat.')
output linkedBackend object = {
  enabled: !empty(thesisApiResourceId)
  name: linkedBackendName
  region: thesisApiRegion
  backendResourceId: thesisApiResourceId
}

@description('Custom-domain binding request for the Static Web App managed certificate. Read the validation token with az staticwebapp hostname show; Bicep does not export the generated token.')
output customDomainBinding object = {
  hostname: customDomainName
  bound: !empty(customDomainName)
  validationMethod: customDomainValidationMethod
}