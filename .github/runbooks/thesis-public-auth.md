# Thesis Public Authentication Bootstrap

This runbook is the one-time, tenant-admin bootstrap that makes the Thesis UI
publicly reachable with invite-only multi-user authentication. It owns the
identity and mail surfaces that no Bicep deployment can create:

- **Entra External ID (CIAM) tenant and app registration** (`fca-thesis-web`):
  SPA authorization-code + PKCE, the API scope/audience the Thesis API
  validates, email/password user flows, and Google/GitHub sign-in.
- **Azure Communication Services email**: the service, sender domain, and the
  Key Vault reference the mailer resolves.
- **Azure Static Web Apps (Standard)**: the SPA host, its deployment token, the
  custom domain, and the free managed certificate.
- **The non-secret deployment configuration** that wires the above into the
  GitHub Environment and the Bicep parameters, and the security verification
  that must pass before the site is announced.

It is grounded in the accepted decision
`docs/adr/adr-001-invite-only-public-multi-user-access-for-thesis.md` (ADR-001)
and is consistent with `infra/modules/thesis.bicep`,
`.github/architecture/fca-thesis.md`, `.github/runbooks/domain-hosting.md`,
`.github/runbooks/entra-app-role-bootstrap.md`, and
`.github/runbooks/github-azure-oidc-bootstrap.md`. It is intentionally not
Bicep: Entra objects are tenant-scoped, the External ID tenant is a separate
directory, and the deployer identity is deliberately least-privileged.

No secret value appears in this runbook, in Git, in Bicep parameters, or in
task evidence. Every credential below is either entered at a masked prompt or
read from Azure into a Key Vault secret or a GitHub Environment secret without
being printed.

## Placeholders And Preconditions

Substitute these operator values. They are non-secret or masked; none is a
credential.

| Placeholder | Meaning | Example |
| --- | --- | --- |
| `$repo` | GitHub repository | `Carson-Fricke/FCA` |
| `$environment` | GitHub Environment | `dev` |
| `$subscriptionId` | Azure subscription that will host ACS and SWA | `<subscription-id>` |
| `$resourceGroup` | Resource group for ACS and SWA (`fca-rg-westus2` in dev) | `<resource-group>` |
| `$location` | Azure region for SWA and ACS metadata | `<azure-region>` |
| `$vault` | Existing Thesis environment Key Vault | `<key-vault-name>` |
| `$ciamTenantId` | The new External ID tenant directory ID | `<ciam-tenant-id>` |
| `$webAppName` | SPA app registration display name | `fca-thesis-web` |
| `$swaName` | Static Web App name | `fca-thesis-web` |
| `$thesisCustomDomain` | Operator-owned site hostname | `thesis.example.com` |
| `$acsEmailDomain` | Operator-owned sender domain | `thesis.example.com` |
| `$thesisOwnerEmail` | Seeded owner account address (personal data, not a secret) | `<owner-email>` |

The operator needs: authorization to create an External ID tenant in the
organization, `Application Administrator` (or equivalent) in that tenant,
`Contributor` on the target resource group, `Key Vault Secrets Officer` on
`$vault`, and GitHub repository administration for Environment variables and
secrets.

```powershell
$repo = 'Carson-Fricke/FCA'
$environment = 'dev'
$subscriptionId = '<subscription-id>'
$resourceGroup = '<resource-group>'
$location = '<azure-region>'
$vault = '<key-vault-name>'
$ciamTenantId = '<ciam-tenant-id>'
$webAppName = 'fca-thesis-web'
$swaName = 'fca-thesis-web'
$thesisCustomDomain = 'thesis.example.com'
$acsEmailDomain = 'thesis.example.com'
$thesisOwnerEmail = '<owner-email>'
$mailerSecretName = 'thesis-acs-email-connection-string'
```

Register the two resource providers this bootstrap adds, so the first
deployment does not fail on an unregistered namespace. Registration is a
subscription-scope action performed by an authorized operator; it is not a
recurring deployer permission.

```powershell
az account set --subscription $subscriptionId
foreach ($namespace in @('Microsoft.Communication', 'Microsoft.Web')) {
  az provider register --namespace $namespace --wait --only-show-errors --output none
  if ($LASTEXITCODE -ne 0) { throw "Provider registration failed: $namespace" }
  az provider show --namespace $namespace --query '{namespace:namespace,state:registrationState}' --output table
}
```

**Operator checkpoint:** confirm both namespaces report `Registered`, not
`Registering`, before continuing.

## Step 1 - Create Or Reuse The Entra External ID Tenant

The Thesis public release uses a **separate External ID (CIAM) tenant**, not the
workforce tenant that holds the platform API applications. Registering
`fca-thesis-web` in the workforce tenant would let its users sign in with
corporate accounts and would blur the invite-only boundary; create the external
tenant instead.

Creating an external tenant is an interactive tenant-admin action. The Azure
CLI and Graph surface for CIAM directory creation has changed across previews,
so this step is performed in the Microsoft Entra admin center
(**Manage tenants > Create > Microsoft Entra External ID**), then the directory
ID is captured for the remaining steps.

```powershell
# After the tenant exists, sign in to it and capture its immutable directory ID.
az login --tenant '<ciam-tenant-domain>.onmicrosoft.com' --only-show-errors --output none
$ciamTenantId = az account show --query tenantId --output tsv
az account show --query '{tenantId:tenantId,tenantName:name,uniqueId:uniqueId}' --output table
```

**Operator checkpoint:** the displayed `tenantId` must match the External ID
tenant, and this shell session must remain signed in to that tenant for Steps 2
and 3. Do not run the app-registration commands against the workforce tenant.
If the organization requires the CIAM tenant to be created by a higher
authority, record the tenant ID, owner, and creation date in the deployment
record rather than storing a credential.

## Step 2 - Register The SPA And Expose The Thesis API

Register one SPA application in the External ID tenant. It is the browser
client (authorization-code + PKCE, no client secret) and the API whose audience
the Thesis backend validates.

```powershell
az login --tenant $ciamTenantId --only-show-errors --output none

$webApp = az ad app create `
  --display-name $webAppName `
  --sign-in-audience AzureADMyOrg `
  --output json | ConvertFrom-Json
$webAppClientId = $webApp.appId
$webAppObjectId = $webApp.id

# The client (SPA) and the API share one registration. A separate API
# registration is not required for the first release.
$sp = az ad sp create --id $webAppClientId --output json | ConvertFrom-Json
```

Configure the SPA redirect URIs (exact origins only; no wildcards) and the API
scope. The Application ID URI makes the API a named resource; the
`Thesis.Access` delegated scope is what an interactive token requests.

```powershell
$swaDefaultHost = az staticwebapp show `
  --name $swaName `
  --resource-group $resourceGroup `
  --query defaultHostname `
  --output tsv 2>$null

$spaRedirectUris = @(
  "https://$thesisCustomDomain",
  'http://localhost:5173'
)
if ($swaDefaultHost) { $spaRedirectUris += "https://$swaDefaultHost" }

$apiScopeId = [guid]::NewGuid().ToString()
$appBody = [ordered]@{
  identifierUris = @("api://$webAppClientId")
  spa            = @{ redirectUris = $spaRedirectUris }
  api            = @{
    oauth2PermissionScopes = @(
      [ordered]@{
        id                      = $apiScopeId
        adminConsentDescription = 'Access the Thesis API as the signed-in user.'
        adminConsentDisplayName = 'Access Thesis API'
        userConsentDescription  = 'Access the Thesis API on your behalf.'
        userConsentDisplayName  = 'Access Thesis API'
        value                   = 'Thesis.Access'
        type                    = 'User'
        isEnabled               = $true
      }
    )
  }
} | ConvertTo-Json -Depth 8

$appBodyPath = Join-Path ([IO.Path]::GetTempPath()) 'thesis-web-app.json'
try {
  Set-Content -LiteralPath $appBodyPath -Value $appBody -Encoding UTF8
  az rest `
    --method PATCH `
    --url "https://graph.microsoft.com/v1.0/applications/$webAppObjectId" `
    --headers 'Content-Type=application/json' `
    --body "@$appBodyPath" `
    --only-show-errors `
    --output none
  if ($LASTEXITCODE -ne 0) { throw 'App registration update failed' }
} finally {
  Remove-Item -LiteralPath $appBodyPath -Force -ErrorAction SilentlyContinue
}
```

Set the local development origin to HTTPS before it is used for a non-loopback
deployment. The browser security invariants in `thesis.auth.session` require
exact origins: they reject a wildcard, reject a non-HTTPS origin on a
non-loopback runtime, and require `HttpOnly`, `Secure`, and a same-site cookie
with CSRF protection. `http://localhost:5173` is acceptable only for loopback
local mode; the hosted origin must be `https://` and exact.

**Operator checkpoint:** enumerate the redirect URIs and confirm every entry is
an exact `https://` origin used by the deployment or local dev, with no
wildcard, path, query, or fragment.

```powershell
az ad app show `
  --id $webAppClientId `
  --query '{displayName:displayName,appId:appId,signInAudience:signInAudience,identifierUris:identifierUris,spa:spa.redirectUris}' `
  --output json
az ad app show `
  --id $webAppClientId `
  --query 'api.oauth2PermissionScopes[].{value:value,type:type,enabled:isEnabled}' `
  --output table
```

The audience value to give the deployment is the `aud` claim the issued access
token actually carries. In an External ID tenant this is normally
`api://<webAppClientId>` after the Application ID URI is set, but token `aud`
formatting has varied by tenant configuration. This is an **operator check**:
decode one access token for the API and record its `aud`, `iss`, `scp`, and
`roles` claims, then set `THESIS_API__ENTRA_AUDIENCE` to the exact `aud`. Never
paste a token or a decoded token containing personal data into this repository
or task evidence; record only the claim shape.

> **Operator check - GitHub as a social identity provider.** Google federation
> for Entra External ID is a first-class portal flow. GitHub is **not** a
> built-in External ID social provider at the time of writing; if Google and
> email/password alone are insufficient, GitHub likely requires a **custom
> OpenID Connect identity provider** and its own app registration in GitHub.
> Confirm the currently supported provider list in the Entra admin center and
> treat GitHub as optional for the initial release rather than asserting it is
> available.

## Step 3 - Configure Sign-In Flows And Identity Providers

Create the sign-up/sign-in user flow that the SPA uses, and disable
self-service sign-up so no uninvited account can be created. Account and
invite authorization is app-owned (ADR-001): the IdP proves identity, while
`thesis.identity` decides whether the identity has an invite or is the seeded
owner.

In the Entra admin center for the External ID tenant:

1. **External Identities > User flows > New user flow**, type
   **Sign up and sign in**, and enable **Email with password**.
2. Set the user-flow **self-service sign-up** behavior to disabled
   (invite-only). Record the flow name; it is non-secret configuration.
3. **External Identities > All identity providers** and add Google.
   Google requires an operator-owned Google Cloud OAuth client; enter the
   client ID and secret directly into the portal, never into Git or a Bicep
   parameter.
4. Re-confirm whether GitHub is offered natively. If it is not, add it as a
   custom OIDC identity provider and validate the issuer, authorization,
   token, and userinfo endpoints with a test sign-in. This is an operator check,
   not a guaranteed provider.
5. In the app registration **Authentication > Single-page application**,
   confirm the flow is authorization-code + PKCE and that implicit grant is
   **not** enabled.
6. **API permissions**: add the `Thesis.Access` delegated scope exposed in
   Step 2 and grant tenant-wide consent if the tenant policy requires it.

**Operator checkpoint:** completing a sign-in with a test account that is
**not** the seeded owner and has **no** invite must authenticate at the IdP
(proving the flow works) and then be rejected by the Thesis API when it tries
to establish a session. If the API issues a session to an uninvited account,
stop and fix authorization before exposing the site.

## Step 4 - Azure Communication Services Email

Create the email service, a sender domain, the sender address, and the Key
Vault reference the mailer resolves. The app sends invite links; it never
stores or logs a raw credential.

```powershell
az account set --subscription $subscriptionId

$acsName = 'fca-thesis-acs'
$acsEmailServiceName = 'fca-thesis-email'
$acsDataLocation = 'United States'   # choose a reviewed ACS data location

az communication create `
  --name $acsName `
  --resource-group $resourceGroup `
  --location global `
  --data-location $acsDataLocation `
  --only-show-errors `
  --output none

az communication email create `
  --name $acsEmailServiceName `
  --resource-group $resourceGroup `
  --location global `
  --data-location $acsDataLocation `
  --only-show-errors `
  --output none
```

Add the sender domain. The Azure-managed subdomain is the zero-DNS option; the
operator-owned domain is required for a branded sender and for a verified
`From` address.

```powershell
# Option A - Azure-managed sender domain (no DNS records to create).
az communication email domain create `
  --email-service-name $acsEmailServiceName `
  --resource-group $resourceGroup `
  --location global `
  --name AzureManagedDomain `
  --domain-management AzureManaged `
  --only-show-errors `
  --output none

# Option B - Operator-owned sender domain (requires DNS verification).
az communication email domain create `
  --email-service-name $acsEmailServiceName `
  --resource-group $resourceGroup `
  --location global `
  --name $acsEmailDomain `
  --domain-management CustomerManaged `
  --only-show-errors `
  --output none
```

For Option B, read the domain's verification records and create them at the
registrar (GoDaddy per `domain-hosting.md`). The names and values come from
Azure; do not guess them.

```powershell
az communication email domain show `
  --email-service-name $acsEmailServiceName `
  --resource-group $resourceGroup `
  --name $acsEmailDomain `
  --query '{domainName:domainName,mailFrom:mailFrom,domainVerification:verificationRecords.Domain,spf:verificationRecords.SPF,dkim:verificationRecords.DKIM,dkim2:verificationRecords.DKIM2}' `
  --output json
```

Create the domain records in the registrar, then trigger verification and
create the sender username after all states are `Verified`.

```powershell
foreach ($verificationType in @('Domain', 'SPF', 'DKIM', 'DKIM2')) {
  az communication email domain initiate-verification `
    --email-service-name $acsEmailServiceName `
    --resource-group $resourceGroup `
    --domain-name $acsEmailDomain `
    --verification-type $verificationType `
    --only-show-errors `
    --output none
}

az communication email domain sender-username create `
  --email-service-name $acsEmailServiceName `
  --resource-group $resourceGroup `
  --domain-name $acsEmailDomain `
  --name 'DoNotReply' `
  --username 'DoNotReply' `
  --display-name 'FCA Thesis' `
  --only-show-errors `
  --output none
```

Store the ACS connection string directly into Key Vault without printing it.
The mailer reads a versionless Key Vault reference (`secretRef`), exactly like
the existing `thesis-api-bearer-token`; a raw value never appears in Git,
Bicep parameters, environment variables, command output, or logs.

```powershell
$acsCredentialPath = Join-Path ([IO.Path]::GetTempPath()) ([IO.Path]::GetRandomFileName())
try {
  az communication list-key `
    --name $acsName `
    --resource-group $resourceGroup `
    --query primaryConnectionString `
    --output tsv |
    Out-File -LiteralPath $acsCredentialPath -Encoding utf8 -NoNewline
  if ($LASTEXITCODE -ne 0) { throw 'Failed to read the ACS primary connection string' }
  az keyvault secret set `
    --vault-name $vault `
    --name $mailerSecretName `
    --file $acsCredentialPath `
    --encoding utf-8 `
    --only-show-errors `
    --output none
  if ($LASTEXITCODE -ne 0) { throw 'Failed to store the mailer secret' }
} finally {
  Remove-Item -LiteralPath $acsCredentialPath -Force -ErrorAction SilentlyContinue
}
```

Confirm the secret exists without reading its value.

```powershell
az keyvault secret show `
  --vault-name $vault `
  --name $mailerSecretName `
  --query '{name:name,enabled:attributes.enabled,created:attributes.created}' `
  --output table
```

**Operator checkpoint:** the mailer secret name must match the name the Thesis
deployment actually references. Confirm it against the deployment task that
wires the mailer; do not assume this runbook's `$mailerSecretName` is final.
If the deployment instead uses the Thesis runtime managed identity against the
ACS endpoint (no connection string), the operator must grant that identity the
appropriate Communication Services role and this Key Vault secret is not
created. Record which path was chosen.

## Step 5 - Azure Static Web Apps And The Deployment Token

Create the Standard-tier Static Web App, capture its default hostname, and
store the deployment token as a GitHub Environment secret. The token is a
credential: read it into a variable and pipe it directly into
`gh secret set`; never echo it, never pass it on a command line, and never let
an unfiltered `az` response print it.

```powershell
az staticwebapp create `
  --name $swaName `
  --resource-group $resourceGroup `
  --location $location `
  --sku Standard `
  --only-show-errors `
  --output none

$swaDefaultHost = az staticwebapp show `
  --name $swaName `
  --resource-group $resourceGroup `
  --query defaultHostname `
  --output tsv

az staticwebapp secrets list `
  --name $swaName `
  --resource-group $resourceGroup `
  --query 'properties.apiKey' `
  --output tsv |
  gh secret set THESIS_SWA_DEPLOYMENT_TOKEN --env $environment --repo $repo
if ($LASTEXITCODE -ne 0) { throw 'Failed to store the Static Web App deployment token' }
```

Link the internal Thesis Container App backend so the browser sees one origin.
The SPA calls the API under the `/api/*` route; the linked backend proxies to
the Container App. The Container App keeps internal ingress with
`allowInsecure=false` and does not bind a public custom hostname.

```powershell
$thesisApiAppName = 'fca-ml-dev-thesis-api'   # composition-derived name
$backendResourceId = az containerapp show `
  --name $thesisApiAppName `
  --resource-group $resourceGroup `
  --query id `
  --output tsv

az staticwebapp backends link `
  --name $swaName `
  --resource-group $resourceGroup `
  --backend-resource-id $backendResourceId `
  --backend-host-header $thesisApiAppName `
  --only-show-errors `
  --output none
```

> **Operator check - internal backend connectivity.** Static Web Apps linked
> backends to a Container App with **internal** ingress may require the
> environment's private reachability or a public backend endpoint, depending on
> the current platform support. Confirm the supported connectivity mode before
> switching the Container App to internal ingress, and record the chosen
> posture. Do not temporarily enable external ingress on the Container App just
> to make linking succeed; that reopens the surface ADR-001 closed.

Also clear any existing public Container App hostname binding when SWA takes
over the site hostname. `.github/runbooks/domain-hosting.md` binds a custom
domain directly to the Container App; with SWA in front, that binding must not
also exist.

```powershell
# Confirm the composition is not binding the public hostname to the API.
az containerapp show `
  --name $thesisApiAppName `
  --resource-group $resourceGroup `
  --query '{external:properties.configuration.ingress.external,customDomains:properties.configuration.ingress.customDomains}' `
  --output json
```

## Step 6 - Custom Domain, DNS, And Managed Certificate

Bind the operator-owned hostname to the Static Web App, then create the DNS
records the SWA validation flow reports, and let Azure issue the free managed
certificate.

```powershell
az staticwebapp hostname set `
  --name $swaName `
  --resource-group $resourceGroup `
  --hostname $thesisCustomDomain `
  --validation-method CNAME `
  --only-show-errors `
  --output none

az staticwebapp hostname list `
  --name $swaName `
  --resource-group $resourceGroup `
  --query '[].{name:name,status:status,validationToken:validationToken}' `
  --output json
```

Create the records at the registrar (GoDaddy in
`.github/runbooks/domain-hosting.md`). Enter only the host part; the registrar
appends the zone.

| Type | Name | Value | Purpose |
| --- | --- | --- | --- |
| `CNAME` | `thesis` | `$swaDefaultHost`, e.g. `<name>.<region>.azurestaticapps.net` | Routes the hostname to the Static Web App |
| `TXT` | `_dnsauth.thesis` | The validation token from `hostname list` | Proves domain ownership to the managed certificate |

**Operator checkpoint:** the TXT record name is the exact name Azure reports.
Static Web Apps custom-domain validation historically uses
`_dnsauth.<subdomain>`; the `asuid.<subdomain>` record in `domain-hosting.md`
belongs to the Container Apps hostname binding and is **not** used here. If the
SWA flow surfaces a different validation record name or token, use what Azure
reports rather than copying the Container Apps record. The token must match
exactly; quotes, whitespace, or truncation leave the certificate pending.

Verify DNS before waiting on issuance.

```powershell
Resolve-DnsName $thesisCustomDomain -Type CNAME
Resolve-DnsName "_dnsauth.$($thesisCustomDomain.Split('.')[0])" -Type TXT
```

**Operator checkpoint:** both records resolve with the values from
`hostname list`, and `az staticwebapp hostname list` reports the custom domain
as validated with a managed certificate. Do not announce the site until the
certificate is issued.

## Step 7 - Configuration To Wire

These are the non-secret values the UI build/deploy workflow and the Bicep
composition consume. Set them on the GitHub Environment, not at repository
scope. The owner email is personal data and must not be committed even though
it is not a credential.

| Name | Kind | Consumer | Value |
| --- | --- | --- | --- |
| `THESIS_TENANT_ID` | Environment variable | Container App / UI build | `$ciamTenantId` |
| `THESIS_WEB_CLIENT_ID` | Environment variable | UI build | `$webAppClientId` |
| `THESIS_API_AUDIENCE` | Environment variable (Bicep `thesisApiEntraAudience`) | Thesis API | the exact token `aud` from Step 2 |
| `THESIS_SWA_NAME` | Environment variable | Deploy workflow | `$swaName` |
| `THESIS_CUSTOM_DOMAIN` | Environment variable | SWA custom domain | `$thesisCustomDomain` |
| `THESIS_OWNER_EMAIL` | Environment variable | `thesis.identity` owner seed | `$thesisOwnerEmail` |
| `THESIS_API_APP_NAME` | Environment variable | Linked backend | derived Thesis API app name |
| `THESIS_EMAIL_SENDER_ADDRESS` | Environment variable | Mailer | `DoNotReply@$acsEmailDomain` |
| `THESIS_EMAIL_ACS_ENDPOINT` | Environment variable | Mailer (non-secret endpoint only) | ACS endpoint URL |

Secrets, and where they live:

| Secret | Store | Consumer |
| --- | --- | --- |
| `THESIS_SWA_DEPLOYMENT_TOKEN` | GitHub Environment secret | UI deploy workflow |
| `thesis-acs-email-connection-string` (or the chosen mailer secret name) | Azure Key Vault, versionless `secretRef` | Thesis mailer |
| Existing `thesis-api-bearer-token` | Azure Key Vault, versionless `secretRef` | Thesis API transition auth |

```powershell
$environmentValues = [ordered]@{
  THESIS_TENANT_ID            = $ciamTenantId
  THESIS_WEB_CLIENT_ID        = $webAppClientId
  THESIS_API_AUDIENCE         = '<aud-from-decoded-token>'
  THESIS_SWA_NAME             = $swaName
  THESIS_CUSTOM_DOMAIN        = $thesisCustomDomain
  THESIS_OWNER_EMAIL          = $thesisOwnerEmail
  THESIS_API_APP_NAME         = $thesisApiAppName
  THESIS_EMAIL_SENDER_ADDRESS = "DoNotReply@$acsEmailDomain"
  THESIS_EMAIL_ACS_ENDPOINT   = '<acs-endpoint-url>'
}
foreach ($entry in $environmentValues.GetEnumerator()) {
  $entry.Value | gh variable set $entry.Key --env $environment --repo $repo
  if ($LASTEXITCODE -ne 0) { throw "Failed to set $($entry.Key)" }
}
```

**Operator checkpoint:** changing `THESIS_API_AUDIENCE` from empty to a
non-empty value flips the Thesis API from static-bearer-only to Entra audience
mode (`infra/modules/thesis.bicep`). Do not set it until the audience is
confirmed from a real token and the browser sign-in path can mint that token;
an incorrect audience makes every protected route fail closed with 401.

## Step 8 - Verification

All checks below disclose identifiers and states only. Never print tokens,
connection strings, secret values, or unfiltered deployment responses.

```powershell
# Tenant and app registration (Step 2).
az account show --query '{tenantId:tenantId,tenantName:name}' --output table
az ad app show --id $webAppClientId `
  --query '{displayName:displayName,appId:appId,signInAudience:signInAudience,identifierUris:identifierUris,spa:spa.redirectUris}' `
  --output json
az ad app show --id $webAppClientId `
  --query 'api.oauth2PermissionScopes[].{value:value,type:type,enabled:isEnabled}' `
  --output table

# Email domain and sender (Step 4).
az communication email domain show `
  --email-service-name $acsEmailServiceName `
  --resource-group $resourceGroup `
  --name $acsEmailDomain `
  --query '{domainName:domainName,mailFrom:mailFrom,domain:verificationStates.Domain.status,spf:verificationStates.SPF.status,dkim:verificationStates.DKIM.status}' `
  --output table
az communication email domain sender-username list `
  --email-service-name $acsEmailServiceName `
  --resource-group $resourceGroup `
  --domain-name $acsEmailDomain `
  --query '[].{username:username,displayName:displayName}' `
  --output table
az keyvault secret show --vault-name $vault --name $mailerSecretName `
  --query '{name:name,enabled:attributes.enabled}' --output table

# Static Web App and custom domain (Steps 5 and 6).
az staticwebapp show --name $swaName --resource-group $resourceGroup `
  --query '{name:name,defaultHostname:defaultHostname,sku:sku.name,customDomains:customDomains}' `
  --output json
Resolve-DnsName $thesisCustomDomain -Type CNAME
Resolve-DnsName "_dnsauth.$($thesisCustomDomain.Split('.')[0])" -Type TXT
```

Public HTTP checks. `/health` is intentionally public; every `/v1/*` route must
return 401 to an anonymous caller. Run these from a network path that can reach
the site (and, for the API, the correct ingress).

```powershell
# SPA origin serves the app and the API fails closed for anonymous callers.
(Invoke-WebRequest -Uri "https://$thesisCustomDomain" -UseBasicParsing).StatusCode
try {
  Invoke-WebRequest -Uri "https://$thesisCustomDomain/v1/auth/check" -UseBasicParsing | Out-Null
} catch {
  $_.Exception.Response.StatusCode.value__
}
try {
  Invoke-WebRequest -Uri "https://$thesisCustomDomain/api/health" -UseBasicParsing | Out-Null
} catch {
  $_.Exception.Response.StatusCode.value__
}
```

Acceptance for this bootstrap:

- The site origin returns 200 over TLS on the custom domain with a managed
  certificate, and the SPA loads.
- `/v1/auth/check` (through the linked backend route the app uses) returns 401
  to an anonymous caller; `/health` on the API returns 200.
- A test identity that is **not** the seeded owner and has **no** invite
  authenticates at the IdP but receives no Thesis session.
- An invited identity signs in, redeems a single-use invite link, and receives
  an `HttpOnly; Secure; SameSite` session cookie bound to its workspace.
- The seeded owner email, on first sign-in, claims the owner role, and existing
  `local-operator` workspaces are visible only to that owner.

**Operator checkpoint:** "only invited accounts can sign in" is enforced in
two layers. If either layer is off - sign-up enabled at the IdP, or the API
issuing sessions without an invite/owner match - the site is not compliant.
Re-test both layers after any user-flow or identity-provider change.

## Security Checklist

| Requirement (ADR-001 / `thesis.auth.session`) | Evidence to record |
| --- | --- |
| No secret in Git, Bicep parameters, variables, logs, or task evidence | `THESIS_SWA_DEPLOYMENT_TOKEN` is a GitHub secret; mailer credential is Key Vault-only; `git grep` finds no secret value; no unfiltered `az` output retained |
| Invite and session tokens hashed at rest | `thesis.identity` stores only hashes; the raw invite token exists only in the emailed link; no raw-token column, row, or log field |
| HttpOnly / Secure / SameSite cookie with CSRF | Non-loopback `RuntimeSecurity` requires `csrf_protection`, `http_only_cookies`, `secure_cookies`, and `same_site != none`; the hosted session cookie shows `HttpOnly`, `Secure`, `SameSite=Lax` |
| Strict redirect-URI and origin allowlists | `az ad app show` SPA redirect URIs and `BrowserSecurity.allowed_origins` are exact HTTPS origins, no wildcard/path/query |
| Rate limits | Sign-in callbacks, invite redemption, and email sends are throttled and fail closed |
| Audit events | Login, invite creation/redemption, consent, and sign-out are recorded |
| TLS everywhere | SWA managed certificate; Container App `allowInsecure=false` |
| Workspace authorization per request | `WorkspaceAuthorizer` runs on every request; anonymous and cross-workspace access fail closed |
| No open signup / no multi-tenancy | User-flow self-service sign-up disabled; organization/multi-tenant collaboration remains a non-goal |

## Rollback And Removal

- **Deployment rollback:** clear `THESIS_API_AUDIENCE` first to return the API
  to static-bearer-only auth, then remove the SWA deployment and Environment
  variables. Do not leave an audience configured while the browser sign-in path
  is broken.
- **Remove the custom domain and DNS records (operator):**

  ```powershell
  az staticwebapp hostname delete `
    --name $swaName `
    --resource-group $resourceGroup `
    --hostname $thesisCustomDomain `
    --yes
  ```

  Then remove the CNAME and `_dnsauth` TXT records at the registrar.
- **Remove the Static Web App:**

  ```powershell
  az staticwebapp delete --name $swaName --resource-group $resourceGroup --yes
  ```
- **Remove the email domain and service (operator):** delete the sender
  username and domain, then the ACS email service and communication resource.
  Revoke the Key Vault secret (`thesis-acs-email-connection-string`) by
  disabling its versions rather than deleting the vault.
- **Remove the app registration and tenant objects (External ID admin):**
  delete the SPA application and its service principal, then the user flow and
  identity providers. Deleting the External ID tenant is a separate,
  high-impact tenant-admin action; do not delete it as routine cleanup.
- **Remove GitHub configuration:** delete `THESIS_SWA_DEPLOYMENT_TOKEN` and the
  `THESIS_*` Environment variables for the affected Environment.

## Diagnosis

- **`AADSTS50011` redirect mismatch:** the deploy origin is not an exact entry
  in `spa.redirectUris`. Add the precise HTTPS origin; never add a wildcard.
- **`invalid audience` / repeated 401 after sign-in:** `THESIS_API_AUDIENCE`
  does not equal the token `aud`. Decode one token, confirm `aud`, `iss`, and
  the delegated `scp` or app `roles` claim, and set the exact value. Confirm
  the API accepts the claim the interactive token carries.
- **Sign-in succeeds but no session is issued:** the account has no invite and
  is not the seeded owner. This is the intended fail-closed result; issue an
  invite or correct the owner email.
- **Email domain stuck in `NotStarted`/`Pending`:** the domain, SPF, or DKIM
  DNS records are missing, quoted, truncated, or wrong. Re-read them from
  `verificationRecords` and re-run `initiate-verification`.
- **SWA certificate stays pending:** the CNAME or `_dnsauth` TXT record is
  wrong or not propagated. Re-read the values from `hostname list` and correct
  the registrar; issuance resumes automatically.
- **Cookie is set but the API rejects the session:** the browser origin is not
  in `BrowserSecurity.allowed_origins`, the transport is not TLS on a
  non-loopback runtime, or the CSRF token is missing. Fix the deployment
  configuration; do not weaken the cookie or CSRF invariants.
- **Linked backend returns 502/503:** the Container App is scaled to zero or
  unreachable from the SWA backend path. Confirm backend connectivity mode and
  the API's health; do not enable public Container App ingress to work around
  it.
- **Protected route returns 200 anonymously:** treat as a security failure.
  Clear `THESIS_API_AUDIENCE`, disable public exposure, and investigate the
  authentication boundary before re-enabling.

## Open Operator Checks

1. **CIAM tenant creation surface.** Confirm the current Microsoft-supported
   way to create the External ID tenant (portal vs CLI/Graph) before automating
   any part of it; this runbook assumes the portal.
2. **GitHub identity provider.** Confirm whether External ID now offers GitHub
   natively; otherwise treat it as a custom OIDC provider and validate with a
   test sign-in.
3. **Token audience and claim.** Record the actual `aud` and whether
   interactive access uses the delegated `scp` scope or an app `roles` claim,
   then align the API configuration and the Bicep `thesisApiEntraAudience`.
4. **SWA validation record name.** Use the exact name Azure returns; do not
   copy the Container Apps `asuid` record from `domain-hosting.md`.
5. **SWA-to-internal-Container-App linked backend.** Confirm the supported
   connectivity posture for internal ingress and record it.
6. **Mailer credential path.** Confirm the final Key Vault secret name against
   the deployment task, or the managed-identity alternative and its role grant.
7. **Owner email handling.** Keep `THESIS_OWNER_EMAIL` out of Git and task
   evidence; it is personal data even though it is not a credential.
