# Thesis Public Domain Hosting And TLS

The Thesis site is served by the Thesis API Container App on the Container
Apps default FQDN. This runbook publishes that app on an operator-owned
public hostname (for example `thesis.example.com`) with a free Azure-managed
TLS certificate. It separates what the repository automates from what only
the operator can do:

- **Repository automation** (already wired): `infra/modules/thesis.bicep`
  accepts an empty-by-default `customDomainName` parameter and, when set,
  adds the hostname binding and the `Microsoft.App/managedEnvironments/managedCertificates`
  resource; `.github/workflows/deploy-thesis.yml` gates the hostname behind
  operator variables and runs an optional HTTPS smoke.
- **Operator actions** (never automated, never credential-bearing): creating
  the DNS validation records at the registrar (GoDaddy) and running the
  one-time `az containerapp hostname add` / `az containerapp hostname bind`
  commands below. This runbook authorizes those documented operator actions;
  no FCA automation touches the registrar and no registrar credential is
  stored, generated, or referenced anywhere in this repository.

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
