# Thesis Public Domain Hosting And TLS

Thesis can be published under an operator-owned hostname in one of two
mutually exclusive postures. Pick exactly one; the two must never claim the
same hostname:

- **Direct Container App posture** (Steps 1-5 of this runbook): the Thesis API
  Container App itself takes external ingress and binds the hostname with a
  free Container Apps managed certificate. The browser talks straight to the
  API host and there is no Static Web App. This is the older single-operator
  posture.
- **Static Web App posture** (ADR-001, the public SPA posture): the Azure
  Static Web App is the public origin and owns the custom domain and managed
  certificate, while the Container App keeps **internal** ingress and binds
  no public hostname. The SWA proxies `/api/*` to the Container App through a
  linked backend, so the browser sees one origin. The identity/tenant
  bootstrap that surrounds this posture is
  `.github/runbooks/thesis-public-auth.md`; the SWA DNS records and linked
  backend are in [Static Web App Hostname Binding](#static-web-app-hostname-binding)
  below.

The remainder of this introduction describes the direct posture; the Static
Web App posture reuses the same registrar and TLS principles but different
record names and commands.

The Thesis site is served by the Thesis API Container App on the Container
Apps default FQDN. This runbook publishes that app on an operator-owned
public hostname (for example `thesis.example.com`) with a free Azure-managed
TLS certificate. It separates what the repository automates from what only
the operator can do:

- **Repository automation** (already wired): `infra/modules/thesis.bicep`
  accepts an empty-by-default `customDomainName` parameter and, when set with
  `enableExternalIngress=true`, adds the hostname binding and the
  `Microsoft.App/managedEnvironments/managedCertificates` resource;
  `.github/workflows/deploy-thesis.yml` gates the hostname behind operator
  variables and runs an optional HTTPS smoke.
- **Operator actions** (never automated, never credential-bearing): creating
  the DNS validation records at the registrar (GoDaddy) and running the
  one-time `az containerapp hostname add` / `az containerapp hostname bind`
  commands below. This runbook authorizes those documented operator actions;
  no FCA automation touches the registrar and no registrar credential is
  stored, generated, or referenced anywhere in this repository.

> **Posture reconciliation (T301).** `infra/modules/thesis.bicep` now binds a
> Container App custom domain only when `enableExternalIngress` is `true` and
> `customDomainName` is non-empty. In the Static Web App posture, keep the
> Container App's `thesisCustomDomain` / `THESIS_CUSTOM_DOMAIN` empty, enable
> no public Container App ingress, and set the SWA hostname through the
> `thesisStaticWebAppCustomDomain` module parameter. Setting the same hostname
> on both the Container App and the SWA is unsupported: two resources cannot
> own one public hostname and its DNS records.

Every command below is copy-executable after substituting the placeholders.
The example values follow the current dev baseline in
`.github/runbooks/github-azure-oidc-bootstrap.md`.

## Ingress Posture And Authorization

Enabling a custom hostname publishes the Thesis API surface (`/health` and
`/v1/*`) on public DNS under a domain the operator controls. Treat it as a
reviewed ingress decision, not a convenience:

- The ingress budget gate is `ENABLE_EXTERNAL_API_INGRESS` (market-lake) and
  `THESIS_ENABLE_EXTERNAL_INGRESS` (Thesis) in
  `.github/runbooks/github-azure-oidc-bootstrap.md`. That runbook records the
  current dev authorization (restored 2026-09-07 for local soak tooling) and
  states the standing rule: external API ingress exposes `/health` publicly
  and protected routes behind the single application bearer token, and a
  separate production ingress and threat review is still required.
- `.github/architecture/fca-thesis.md` (Runtime Modes) requires an explicit
  authenticated/TLS deployment mode for any non-loopback binding, and
  requires browser sessions, reconnects, CORS, CSRF, and workspace
  authorization to remain enforced even for the single-operator release.
  The custom hostname changes transport and name only: public routes stay
  bearer/Entra protected. `/health` is intentionally public; every protected
  route must keep returning 401 to anonymous callers.
- A custom domain binds only to **external** ingress in Container Apps. The
  deploy workflow enforces the pairing (see Step 4): setting
  `THESIS_CUSTOM_DOMAIN` without the authorization acknowledgment, or with
  `THESIS_ENABLE_EXTERNAL_INGRESS` not `true`, fails the deployment before
  any Azure change.

The certificate is the free Azure-managed Container Apps certificate: no
`.pfx` file, no password, no `az containerapp ssl upload` (that command is
for uploaded custom certificates and is not used in this posture), and
auto-renewal by the platform. The binding is SNI (`SniEnabled`); the app's
`allowInsecure` stays `false`.

## Step 1 - Read The Azure Values (operator, read-only)

```powershell
$subscriptionId     = '44534e50-b76f-472f-8c13-3f2033e30232'
$resourceGroup      = 'fca-rg-westus2'
$environment        = 'dev'
$prefix             = 'fca-ml'
$thesisApiAppName   = "$prefix-$environment-thesis-api"
$environmentName    = "$prefix-$environment-cae"
$thesisCustomDomain = 'thesis.example.com'   # choose the operator-owned hostname

az account set --subscription $subscriptionId

# Default FQDN (the CNAME target) and the domain-verification ID (the TXT value).
az containerapp show `
  --name $thesisApiAppName `
  --resource-group $resourceGroup `
  --query '{defaultFqdn:properties.configuration.ingress.fqdn,verificationId:properties.customDomainVerificationId,external:properties.configuration.ingress.external}' `
  --output table

# Environment static IP (only needed for an apex/root domain).
az containerapp env show `
  --name $environmentName `
  --resource-group $resourceGroup `
  --query '{staticIp:properties.staticIp,dnsSuffix:properties.defaultDomain}' `
  --output table
```

Record the outputs: `defaultFqdn`, `verificationId`, and (for an apex
domain) `staticIp`. These are non-secret identifiers.

## Step 2 - Create The GoDaddy DNS Records (operator; registrar)

In GoDaddy: **My Products > Domains > DNS > Manage DNS**. GoDaddy appends the
zone automatically, so enter only the host part in the Name field. Use the
GoDaddy default TTL (600 seconds).

**Subdomain (recommended), e.g. `thesis.example.com`:**

| Type | Name (at GoDaddy) | Value | Purpose |
| --- | --- | --- | --- |
| `CNAME` | `thesis` | `defaultFqdn` from Step 1, e.g. `fca-ml-dev-thesis-api.<env-suffix>.azurecontainerapps.io` | Routes the hostname to the Container App |
| `TXT` | `asuid.thesis` | `verificationId` from Step 1 | Proves domain ownership to the managed certificate |

**Apex/root domain, e.g. `example.com`** (a CNAME cannot coexist with the
zone apex, so an A record is required instead):

| Type | Name (at GoDaddy) | Value | Purpose |
| --- | --- | --- | --- |
| `A` | `@` | `staticIp` from Step 1 | Routes the apex hostname to the Container Apps environment |
| `TXT` | `asuid` | `verificationId` from Step 1 | Proves domain ownership to the managed certificate |

Operator notes:

- The `asuid` TXT value must match the verification ID exactly; quotes,
  whitespace, or a truncated value leave the certificate pending forever.
- Remove GoDaddy's default parking/forwarding records for the same host
  (an `A @` "Parked" record conflicts with the apex A record; forwarding
  must be off for the chosen host).
- If the zone publishes CAA records, ensure they permit the Azure-managed
  certificate issuer (DigiCert-rooted Microsoft Azure issuing CAs), for
  example `CAA 0 issue "digicert.com"`; a restrictive CAA set silently
  blocks issuance.
- The DNS records are created by the operator in the GoDaddy web console or
  a registrar tool of their choice. Do not paste registrar credentials into
  this repository, GitHub variables, or task evidence.

## Step 3 - Verify DNS Resolution (operator, read-only)

From the operator's machine (propagation can take several minutes):

```powershell
Resolve-DnsName $thesisCustomDomain -Type CNAME
Resolve-DnsName "asuid.thesis.example.com" -Type TXT
```

Continue only when both records resolve with the Step 1 values.

## Step 4 - Bind The Hostname And The Managed Certificate

Choose one path. Both produce the same state; the workflow path is preferred
because it is idempotent under future deployments.

### Path A - Workflow-managed (preferred)

Set these on the GitHub Environment (Settings > Environments > `dev` >
Add variable); they are public configuration, not secrets:

| Environment variable | Value |
| --- | --- |
| `THESIS_CUSTOM_DOMAIN` | The hostname, e.g. `thesis.example.com` |
| `THESIS_CUSTOM_DOMAIN_AUTHORIZED` | Exactly `true` (records the ingress-authorization acknowledgment) |
| `THESIS_CHECK_CUSTOM_DOMAIN` | `true` to enable the post-deploy custom-hostname smoke (optional) |

Then run `deploy-thesis.yml` (merge a change to the matched paths or use
workflow_dispatch). The Bicep wiring adds the hostname binding and the
managed certificate; the workflow validation fails closed when the
acknowledgment is missing or when `THESIS_ENABLE_EXTERNAL_INGRESS` is not
`true`. The optional smoke curls `https://<hostname>/health` (expect 200)
and `https://<hostname>/v1/auth/check` (expect 401).

### Path B - Operator az commands (pre-wiring or repair)

```powershell
az account set --subscription $subscriptionId

# 1. Add the hostname to the app without a certificate binding.
az containerapp hostname add `
  --name $thesisApiAppName `
  --resource-group $resourceGroup `
  --hostname $thesisCustomDomain `
  --only-show-errors `
  --output none

# 2. Look up or create the free managed certificate and bind it. CNAME
#    validation reads the asuid TXT record from Step 2. No certificate file
#    is uploaded; `az containerapp ssl upload` is not used.
az containerapp hostname bind `
  --name $thesisApiAppName `
  --resource-group $resourceGroup `
  --hostname $thesisCustomDomain `
  --validation-method CNAME `
  --only-show-errors `
  --output none
```

`az containerapp hostname bind` without `--certificate` or `--thumbprint`
creates the managed certificate for the hostname on the environment and
binds it. Certificate issuance polls the DNS records and can take several
minutes after they resolve.

## Step 5 - Validate (read-only and public HTTP checks)

```powershell
# Hostname list and binding state.
az containerapp hostname list --name $thesisApiAppName --resource-group $resourceGroup --output table
az containerapp show `
  --name $thesisApiAppName `
  --resource-group $resourceGroup `
  --query properties.configuration.ingress.customDomains `
  --output json

# Managed-certificate state on the environment.
az containerapp env certificate list `
  --name $environmentName `
  --resource-group $resourceGroup `
  --query "[].{name:name,subjectName:properties.subjectName,validation:properties.domainControlValidation}" `
  --output table

# Public checks from any network path.
(Invoke-WebRequest -Uri "https://$thesisCustomDomain/health" -UseBasicParsing).StatusCode
try { Invoke-WebRequest -Uri "https://$thesisCustomDomain/v1/auth/check" -UseBasicParsing | Out-Null } catch { $_.Exception.Response.StatusCode.value__ }
```

`/health` must return 200 over TLS on the custom hostname, and
`/v1/auth/check` must return 401 to the anonymous caller. Anything else is a
security posture failure, not a convenience issue.

## Static Web App Hostname Binding

This section is the Static Web App posture. It replaces Steps 1-5 for the
public site; the Container App keeps internal ingress and binds no hostname.
The identity, email, and Static Web App bootstrap stays in
`.github/runbooks/thesis-public-auth.md`; this section owns the hosting and TLS
records so the two runbooks do not drift.

### Repository automation (T301)

- `infra/modules/static-web-app.bicep` creates the linked backend
  (`Microsoft.Web/staticSites/linkedBackends`) and the custom-domain binding
  (`Microsoft.Web/staticSites/customDomains`, `validationMethod: 'dns-txt-token'`).
  The linked backend name is the backend resource name, matching what
  `az staticwebapp backends link` derives.
- `infra/modules/thesis.bicep` exposes a non-secret `backend` output
  (`resourceId`, `region`, `name`, `fqdn`, `ingressExternal`) alongside the
  existing `thesisApi` output. The composition passes `backend.resourceId` and
  `backend.region` as `thesisApiResourceId` / `thesisApiRegion`.
- The binding is opt-in and gated: an empty `thesisApiResourceId` creates no
  linked backend and an empty `customDomainName` creates no custom domain, so a
  Thesis-disabled composition is unchanged.

> **Operator check - composition wiring.** Confirm `infra/main.bicep` passes
> `thesisApi.outputs.backend.resourceId` and `.region` into
> `thesis-static-web-app`. If it does not, the module deploys an unlinked host
> with no `/api` backend and the public site cannot reach the API.

### Step A - Read the Static Web App values (operator, read-only)

```powershell
$subscriptionId     = '44534e50-b76f-472f-8c13-3f2033e30232'
$resourceGroup      = 'fca-rg-westus2'
$swaName            = 'fca-ml-dev-thesis-web-<suffix>'   # staticWebAppName
$thesisApiAppName   = 'fca-ml-dev-thesis-api'            # composition-derived
$thesisCustomDomain = 'thesis.example.com'               # operator-owned hostname

az account set --subscription $subscriptionId

# CNAME target, tier, and the current custom-domain state.
az staticwebapp show `
  --name $swaName `
  --resource-group $resourceGroup `
  --query '{defaultHostname:defaultHostname,sku:sku.name,customDomains:customDomains}' `
  --output table

# Linked backend the SWA proxies /api/* to.
az staticwebapp backends show `
  --name $swaName `
  --resource-group $resourceGroup `
  --output json
```

### Step B - Create the registrar records (operator)

In GoDaddy: **My Products > Domains > DNS > Manage DNS**. Enter only the host
part; GoDaddy appends the zone. Use the GoDaddy default TTL (600 seconds).

| Type | Name (at GoDaddy) | Value | Purpose |
| --- | --- | --- | --- |
| `CNAME` | `thesis` | `defaultHostname` from Step A, e.g. `<name>.<region>.azurestaticapps.net` | Routes the hostname to the Static Web App |
| `TXT` | `_dnsauth.thesis` | The `validationToken` from `az staticwebapp hostname show` | Proves domain ownership for the managed certificate |

The exact TXT name and token are whatever Azure reports; `_dnsauth.<host>` is
the current Static Web Apps convention. This is **not** the Container Apps
`asuid.<host>` record from Step 2 - the two postures use different validation
records even for the same hostname.

### Step C - Bind the SWA hostname and managed certificate

Choose one path. Both produce the same state.

**Path A - Bicep (preferred).** Set `staticWebAppName` and the
`thesisStaticWebAppCustomDomain` module parameter (via the non-secret deploy
configuration), then deploy. The module creates the custom-domain binding;
Azure issues and renews the free managed certificate after the Step B records
resolve.

**Path B - operator az commands (repair or pre-wiring).** Static Web Apps
accepts `dns-txt-token` and `cname-delegation`; it does **not** accept
`CNAME` (that value belongs only to the Container Apps commands in Step 4):

```powershell
az staticwebapp hostname set `
  --name $swaName `
  --resource-group $resourceGroup `
  --hostname $thesisCustomDomain `
  --validation-method dns-txt-token `
  --only-show-errors `
  --output none

# Validation token for the _dnsauth TXT record, if it is not already created.
az staticwebapp hostname show `
  --name $swaName `
  --resource-group $resourceGroup `
  --hostname $thesisCustomDomain `
  --query validationToken `
  --output tsv
```

### Step D - Verify (read-only and public HTTP checks)

```powershell
Resolve-DnsName $thesisCustomDomain -Type CNAME
Resolve-DnsName "_dnsauth.$($thesisCustomDomain.Split('.')[0])" -Type TXT

az staticwebapp hostname list --name $swaName --resource-group $resourceGroup --output table
az staticwebapp backends show --name $swaName --resource-group $resourceGroup --output json

# Public origin is the SWA; /api/* reaches the internal API through the linked backend.
# These checks require the Container App to serve its routes under /api (see below).
(Invoke-WebRequest -Uri "https://$thesisCustomDomain" -UseBasicParsing).StatusCode
(Invoke-WebRequest -Uri "https://$thesisCustomDomain/api/health" -UseBasicParsing).StatusCode
try { Invoke-WebRequest -Uri "https://$thesisCustomDomain/api/v1/auth/check" -UseBasicParsing | Out-Null } catch { $_.Exception.Response.StatusCode.value__ }
```

`/api/health` must return 200 (the linked backend reached the internal API)
and `/api/v1/auth/check` must return 401 to an anonymous caller.

### Linked backend connectivity and path limitations

Two constraints from the Static Web Apps API documentation apply here and are
**not** resolved by this task:

1. **Network-isolated backends are not supported.** The Static Web Apps API
   overview states that network-isolated backends are not supported, and the
   Container Apps integration only supports a backend the SWA can reach. The
   Thesis Container App is intended to keep **internal** ingress (ADR-001), so
   the linked backend may simply be unable to reach it. Treat this as an
   **operator check**, not a proven capability:
   - Confirm the currently supported connectivity mode for an internal
     Container App backend before relying on `/api/*`. If private reachability
     is required, it is a separate reviewed change (for example VNet
     integration or a private endpoint on the Container Apps environment); no
     such module exists in this repository and T301 does not add one.
   - Do **not** temporarily enable external ingress on the Container App just
     to make linking succeed. That reopens the public surface ADR-001 closed
     and contradicts the posture in `.github/runbooks/thesis-public-auth.md`.
   - A linked backend returning 502/503 points at connectivity or
     scale-to-zero, not at the public ingress. Confirm the backend path and the
     API health first.
2. **The `/api` prefix is forwarded unchanged.** Static Web Apps matches
   requests whose route starts with `/api` and proxies the **entire path** to
   the linked Container App; the backend must expose the same `/api` prefix.
   As deployed today, `thesis serve` serves `/health`, `/healthz`, and `/v1/*`
   at the root and has no `/api` base path (no `root_path` or prefix option).
   The UI calls the same-origin `/api` base, so the linked backend would send
   `/api/health` and `/api/v1/*`, which the current API does not route. The
   composition or the serve host must mount the API under `/api` (or the SWA
   `/api` client must be reconciled to the backend routes) before the `/api`
   smoke can pass. This is an implementation check the coordinator must carry
   forward; T301 provisions the linked backend only.

### Rollback and removal (Static Web App posture)

```powershell
# Remove the custom domain and its managed certificate.
az staticwebapp hostname delete `
  --name $swaName `
  --resource-group $resourceGroup `
  --hostname $thesisCustomDomain `
  --yes

# Remove the linked backend.
az staticwebapp backends unlink `
  --name $swaName `
  --resource-group $resourceGroup
```

Then remove the CNAME and `_dnsauth` TXT records at the registrar, and clear
the SWA custom-domain parameter so the next deployment removes the binding.
The full Static Web App teardown is in
`.github/runbooks/thesis-public-auth.md`.

## Rollback And Removal

- Clear `THESIS_CUSTOM_DOMAIN` (and the optional check variable) on the
  Environment; the next deployment removes the hostname binding because the
  Bicep default keeps `customDomains` empty.
- Manual unbind and certificate cleanup (operator):

  ```powershell
  az containerapp hostname delete `
    --name $thesisApiAppName `
    --resource-group $resourceGroup `
    --hostname $thesisCustomDomain `
    --yes
  ```

- Remove the Step 2 records in the GoDaddy console (operator).

## Diagnosis

- **Certificate stays pending:** the `asuid` TXT value does not match the
  Step 1 verification ID exactly, or the CNAME does not point at the app's
  default FQDN. Re-read both values with Step 1 and correct the GoDaddy
  records; issuance resumes automatically once validation reads them.
- **Bind fails with a validation error:** DNS has not propagated; wait and
  rerun Step 3 before retrying `az containerapp hostname bind`.
- **HTTP but not HTTPS, or certificate warnings:** the binding is missing or
  issuance has not completed; inspect `customDomains` in Step 5.
- **Protected route no longer returns 401:** stop serving public traffic by
  clearing `THESIS_CUSTOM_DOMAIN` and setting
  `THESIS_ENABLE_EXTERNAL_INGRESS=false`, then investigate the API
  authentication boundary before re-enabling.
