# GitHub Azure OIDC Bootstrap

This runbook separates one-time, human-authorized bootstrap from recurring
GitHub OIDC deployments. It supports the existing `dev` environment and a
separately parameterized `prod` environment without creating an Azure client
secret or committing any secret.

GitHub stores deployment identifiers and the budget contact. Azure Key Vault
stores the Alpaca key, Alpaca secret, and application bearer token. GitHub must
never store those three runtime values, and Azure must never receive them as
Bicep parameters.

## Current Dev Baseline

The following values were confirmed against GitHub, Azure, and the latest
hosted deployment on 2026-09-02. Preserve them when maintaining `dev`; do not
copy subscription, tenant, resource-group, region, or network assumptions into
another environment.

| Setting | Current dev value |
| --- | --- |
| Repository | `Carson-Fricke/FCA` |
| Owner name and immutable ID | `Carson-Fricke`, `48462838` |
| Repository name and immutable ID | `FCA`, `1347935926` |
| GitHub Environment | `dev` |
| Azure subscription | `44534e50-b76f-472f-8c13-3f2033e30232` |
| Microsoft Entra tenant | `6c199962-2933-4a4b-901f-46f1578826c4` |
| Resource group | `fca-rg-westus2` |
| Region | `westus2` |
| Resource prefix | `fca-ml` |
| GitHub deployer client ID | `a3137f67-b9ac-49ee-ac2d-447b55da85a0` |
| Key Vault | `fca-ml-dev-kv-obj3kww` |
| Budget start | `2026-09-01` |
| Key Vault public network | `true` |
| External API ingress | `true` (restored 2026-09-07 for local soak tooling; see Dev Deployment Enablement And Rebootstrap) |
| Ingest schedule and timeout | `0 1 * * *`, `3600` seconds |

The repository OIDC API reports `use_default=true`,
`use_immutable_subject=true`, and this prefix:

```text
repo:Carson-Fricke@48462838/FCA@1347935926
```

The Azure federated credential is named `github-deploy` and is exactly:

```text
issuer:   https://token.actions.githubusercontent.com
audience: api://AzureADTokenExchange
subject:  repo:Carson-Fricke@48462838/FCA@1347935926:environment:dev
```

The `dev` Environment currently has a custom `master`-only deployment branch
policy and one secret named `BUDGET_ALERT_CONTACT_EMAIL`. Required reviewers
are not active: GitHub returned HTTP 422 because this private personal
repository's current plan does not support that protection rule. Therefore the
current dev run has a branch gate, not a manual approval gate. Add a required
reviewer and prevent self-review if repository visibility or plan support
changes.

Hosted run
[`33696588091`](https://github.com/Carson-Fricke/FCA/actions/runs/33696588091)
completed successfully with private ingress, immutable GHCR-to-ACR mirroring,
OIDC login, Bicep deployment, digest checks, `/health` probes, and restoration
to zero minimum API replicas. The current Key Vault uses RBAC, has public
network access enabled, and contains only the expected secret names described
below.

## Trust And Permissions

The one-time maintainer needs:

- Azure CLI access to the intended subscription and an existing resource
  group.
- `Contributor` and `Role Based Access Control Administrator` at that resource
  group, or `Owner` at that or an ancestor scope. `Contributor` alone cannot
  create role assignments.
- Permission to register resource providers at subscription scope, or help
  from a subscription operator who has that permission.
- GitHub repository administration for Actions variables, Environments, OIDC
  settings, and package Actions access.
- `Key Vault Secrets Officer` on the environment vault to seed and rotate
  values. The runtime identity receives only `Key Vault Secrets User`.

The one-time Bicep bootstrap creates or confirms:

- Separate `<prefix>-<environment>-runtime` and
  `<prefix>-<environment>-github-deployer` user-assigned identities.
- The environment-scoped `github-deploy` federated credential.
- A deterministic private Basic Azure Container Registry with its admin
  account disabled.
- Registry-scoped `AcrPull` for the runtime and deployer identities.
- Resource-group `Contributor` for the deployer.
- A resource-group `Role Based Access Control Administrator` assignment whose
  condition permits only `AcrPull`, Storage Blob/Queue/Table data roles, and
  `Key Vault Secrets User`, for any workload principal in the resource group.
  The scope is the resource group so the grant also covers resources later
  deployments create, such as the dedicated Thesis workspace storage account.

The deployer must not receive `Owner`. Recurring deployment passes
`assignGithubDeployerRoles=false`; only the authorized bootstrap owns its
authorization.

Use a distinct prod resource group. Reusing the dev resource group would give
the prod deployer Contributor over dev resources and defeat environment
isolation.

## Select An Environment Profile

Run all PowerShell examples from the repository root. The profile contains
only public identifiers and non-secret deployment choices.

Use these exact values for current dev:

```powershell
$repo = 'Carson-Fricke/FCA'
$subscriptionId = '44534e50-b76f-472f-8c13-3f2033e30232'
$resourceGroup = 'fca-rg-westus2'
$location = 'westus2'
$resourceNamePrefix = 'fca-ml'
$environment = 'dev'
$budgetStartDate = '2026-09-01'
$allowPublicNetworkAccess = 'true'
$enableExternalApiIngress = 'false'
$ingestCronExpression = '0 1 * * *'
$ingestReplicaTimeoutSeconds = '3600'
```

For prod, choose and review every placeholder. These are examples, not FCA
production defaults:

```powershell
$repo = 'Carson-Fricke/FCA'
$subscriptionId = '<prod-subscription-id>'
$resourceGroup = '<prod-resource-group>'
$location = '<prod-azure-region>'
$resourceNamePrefix = '<prod-resource-prefix>'
$environment = 'prod'
$budgetStartDate = '<YYYY-MM-01>'
$allowPublicNetworkAccess = '<true-or-false-after-network-review>'
$enableExternalApiIngress = '<true-or-false-after-ingress-review>'
$ingestCronExpression = '<five-field-UTC-cron>'
$ingestReplicaTimeoutSeconds = '<60-through-86400>'
```

`resourceNamePrefix` must contain 2 through 16 characters and `environment`
must contain 1 through 12. For this composition, use lowercase letters,
numbers, and single hyphens; start with a letter, end with an alphanumeric
character, and keep their combined length at 24 or fewer characters so the
derived Container Apps job name remains valid. Although GitHub encodes a colon
as `%3A` in an OIDC subject, a colon is not valid in the derived Azure resource
names used here. The current Bicep has no Key Vault private endpoint or private
DNS module. Setting `ALLOW_PUBLIC_NETWORK_ACCESS=false` is usable only when
approved private connectivity and DNS are supplied separately. Enabling
external API ingress exposes `/health` publicly and protected routes behind the
single application bearer token; it still requires a separate production
ingress and threat review.

Read the authoritative repository names, casing, and public IDs rather than
typing prod copies from memory:

```powershell
$repoMetadata = gh api "repos/$repo" | ConvertFrom-Json
$githubOwner = $repoMetadata.owner.login
$githubOwnerId = [string] $repoMetadata.owner.id
$githubRepository = $repoMetadata.name
$githubRepositoryId = [string] $repoMetadata.id

$repoMetadata | Select-Object `
  @{n='owner';e={$_.owner.login}}, `
  @{n='ownerId';e={$_.owner.id}}, `
  @{n='repository';e={$_.name}}, `
  @{n='repositoryId';e={$_.id}}
```

These IDs are public identifiers, not credentials.

## Register Azure Providers

Provider registration is a subscription-level bootstrap action, not a
recurring deployer permission. Register every namespace used by the bootstrap
and main Bicep compositions before the first deployment:

```powershell
az account set --subscription $subscriptionId

$providerNamespaces = @(
  'Microsoft.App'
  'Microsoft.Authorization'
  'Microsoft.Consumption'
  'Microsoft.ContainerRegistry'
  'Microsoft.Insights'
  'Microsoft.KeyVault'
  'Microsoft.ManagedIdentity'
  'Microsoft.OperationalInsights'
  'Microsoft.Storage'
)

foreach ($namespace in $providerNamespaces) {
  az provider register `
    --namespace $namespace `
    --wait `
    --only-show-errors `
    --output none
  if ($LASTEXITCODE -ne 0) { throw "Provider registration failed: $namespace" }
}

foreach ($namespace in $providerNamespaces) {
  az provider show `
    --namespace $namespace `
    --query '{namespace:namespace,state:registrationState}' `
    --output table
}
```

All nine namespaces are currently `Registered` in dev. A `Registering` state
is not sufficient for the first full deployment.

## Configure The Immutable OIDC Subject

The repository must use GitHub's default immutable subject. Do not configure
custom claim keys: their subject format will not match the Bicep credential.
Opt in or reaffirm the required mode, then read it back:

```powershell
@{
  use_default = $true
  use_immutable_subject = $true
} | ConvertTo-Json -Compress | gh api `
  --method PUT `
  "repos/$repo/actions/oidc/customization/sub" `
  --input -
if ($LASTEXITCODE -ne 0) { throw 'Failed to configure repository OIDC' }

$oidc = gh api "repos/$repo/actions/oidc/customization/sub" |
  ConvertFrom-Json
if (-not $oidc.use_default -or -not $oidc.use_immutable_subject) {
  throw 'Repository OIDC is not using the immutable default subject'
}

$expectedPrefix =
  "repo:${githubOwner}@${githubOwnerId}/${githubRepository}@${githubRepositoryId}"
if ($oidc.sub_claim_prefix -ne $expectedPrefix) {
  throw "Unexpected immutable prefix: $($oidc.sub_claim_prefix)"
}

$encodedEnvironment = $environment.Replace(':', '%3A')
$expectedSubject = "${expectedPrefix}:environment:${encodedEnvironment}"
$expectedSubject
```

The environment name is included because the deploy job targets a GitHub
Environment. Names and casing must match the GitHub API. A colon in an
environment name is encoded as `%3A`.

For this repository the exact subjects are:

| Environment | Exact immutable subject |
| --- | --- |
| `dev` | `repo:Carson-Fricke@48462838/FCA@1347935926:environment:dev` |
| `prod` | `repo:Carson-Fricke@48462838/FCA@1347935926:environment:prod` |

For a fork, transfer, or different repository, use the API-derived prefix and
do not reuse either FCA subject.

## Deploy The One-Time Azure Bootstrap

Confirm the resource group belongs to the selected subscription and region.
Review `what-if`, then create the identity, credential, ACR, and role
assignments. No command in this section accepts a secret.

If the approved prod resource group does not exist, an authorized subscription
operator creates it first. This operation is idempotent, but run it only after
confirming the selected subscription and approved region:

```powershell
az account set --subscription $subscriptionId
$resourceGroupExists = az group exists --name $resourceGroup
if ($resourceGroupExists -ne 'true') {
  az group create `
    --name $resourceGroup `
    --location $location `
    --only-show-errors `
    --output none
  if ($LASTEXITCODE -ne 0) { throw 'Resource-group creation failed' }
}
```

```powershell
az account set --subscription $subscriptionId
az group show `
  --name $resourceGroup `
  --query '{name:name,location:location,id:id}' `
  --output table

$bootstrapDeployment = if ($environment -eq 'dev') {
  'fca-github-oidc-bootstrap'
} else {
  "fca-$environment-github-oidc-bootstrap"
}

$bootstrapParameters = @(
  "location=$location"
  "resourceNamePrefix=$resourceNamePrefix"
  "environmentName=$environment"
  "githubOwner=$githubOwner"
  "githubOwnerId=$githubOwnerId"
  "githubRepository=$githubRepository"
  "githubRepositoryId=$githubRepositoryId"
)

az deployment group validate `
  --resource-group $resourceGroup `
  --template-file infra/bootstrap-github-oidc.bicep `
  --parameters $bootstrapParameters `
  --only-show-errors `
  --output none
if ($LASTEXITCODE -ne 0) { throw 'Bootstrap validation failed' }

az deployment group what-if `
  --resource-group $resourceGroup `
  --template-file infra/bootstrap-github-oidc.bicep `
  --parameters $bootstrapParameters `
  --only-show-errors
if ($LASTEXITCODE -ne 0) { throw 'Bootstrap what-if failed' }

az deployment group create `
  --name $bootstrapDeployment `
  --resource-group $resourceGroup `
  --template-file infra/bootstrap-github-oidc.bicep `
  --parameters $bootstrapParameters `
  --only-show-errors `
  --output none
if ($LASTEXITCODE -ne 0) { throw 'Bootstrap deployment failed' }

$githubConfiguration = az deployment group show `
  --name $bootstrapDeployment `
  --resource-group $resourceGroup `
  --query properties.outputs.githubConfiguration.value `
  --output json | ConvertFrom-Json

$githubConfiguration | Select-Object `
  environment, azureClientId, azureTenantId, azureSubscriptionId, `
  azureResourceGroup, azureLocation, resourceNamePrefix
```

The selected output fields are non-secret configuration. Do not paste an
unfiltered deployment response into issues, chat, or logs.

Verify the Azure credential character for character:

```powershell
$identityName = "$($resourceNamePrefix.ToLower())-$($environment.ToLower())-github-deployer"
$credential = az identity federated-credential show `
  --resource-group $resourceGroup `
  --identity-name $identityName `
  --name github-deploy `
  --query '{issuer:issuer,audiences:audiences,subject:subject}' `
  --output json | ConvertFrom-Json

if ($credential.issuer -ne 'https://token.actions.githubusercontent.com') {
  throw 'Unexpected federated issuer'
}
if ($credential.audiences.Count -ne 1 -or
    $credential.audiences[0] -ne 'api://AzureADTokenExchange') {
  throw 'Unexpected federated audience'
}
if ($credential.subject -ne $expectedSubject) {
  throw "Subject mismatch: $($credential.subject)"
}
```

Azure role assignments can take several minutes, and occasionally up to 30
minutes, to become effective. Retry authentication after waiting; do not widen
scope or add `Owner` to work around propagation.

## Configure GitHub Environment Values

In **Settings > Environments**, create the exact `$environment` name. Select
**Selected branches and tags**, add only branch `master`, and add a required
reviewer plus prevent-self-review when the repository plan supports them. For
prod, also disable administrator bypass if the available plan and governance
policy permit it.

In **Settings > Secrets and variables > Actions > Variables**, set the one
repository variable:

| Repository variable | Value |
| --- | --- |
| `DEPLOYMENT_ENVIRONMENT` | Exact selected Environment name, `dev` or `prod` |

Set these variables on the selected Environment, not at repository scope:

| Environment variable | Dev value | Prod value |
| --- | --- | --- |
| `AZURE_CLIENT_ID` | `a3137f67-b9ac-49ee-ac2d-447b55da85a0` | Bootstrap output |
| `AZURE_TENANT_ID` | `6c199962-2933-4a4b-901f-46f1578826c4` | Bootstrap output |
| `AZURE_SUBSCRIPTION_ID` | `44534e50-b76f-472f-8c13-3f2033e30232` | Prod ID |
| `AZURE_RESOURCE_GROUP` | `fca-rg-westus2` | `<prod-resource-group>` |
| `AZURE_LOCATION` | `westus2` | `<prod-azure-region>` |
| `RESOURCE_NAME_PREFIX` | `fca-ml` | `<prod-resource-prefix>` |
| `BUDGET_START_DATE` | `2026-09-01` | First day of approved budget period |
| `ALLOW_PUBLIC_NETWORK_ACCESS` | `true` | Reviewed Key Vault network choice |
| `ENABLE_EXTERNAL_API_INGRESS` | `true` | Reviewed API ingress choice (dev restored 2026-09-07 for soak tooling) |
| `INGEST_CRON_EXPRESSION` | `0 1 * * *` | Reviewed five-field UTC cron |
| `INGEST_REPLICA_TIMEOUT_SECONDS` | `3600` | `60` through `86400` |

The following sets the non-secret values from the selected profile without a
temporary file:

```powershell
$environmentValues = [ordered]@{
  AZURE_CLIENT_ID = $githubConfiguration.azureClientId
  AZURE_TENANT_ID = $githubConfiguration.azureTenantId
  AZURE_SUBSCRIPTION_ID = $subscriptionId
  AZURE_RESOURCE_GROUP = $resourceGroup
  AZURE_LOCATION = $location
  RESOURCE_NAME_PREFIX = $resourceNamePrefix
  BUDGET_START_DATE = $budgetStartDate
  ALLOW_PUBLIC_NETWORK_ACCESS = $allowPublicNetworkAccess
  ENABLE_EXTERNAL_API_INGRESS = $enableExternalApiIngress
  INGEST_CRON_EXPRESSION = $ingestCronExpression
  INGEST_REPLICA_TIMEOUT_SECONDS = $ingestReplicaTimeoutSeconds
}

foreach ($entry in $environmentValues.GetEnumerator()) {
  $entry.Value | gh variable set $entry.Key --env $environment --repo $repo
  if ($LASTEXITCODE -ne 0) { throw "Failed to set $($entry.Key)" }
}

$environment | gh variable set DEPLOYMENT_ENVIRONMENT --repo $repo
if ($LASTEXITCODE -ne 0) { throw 'Failed to select deployment environment' }
```

Set the budget contact as the only GitHub Environment secret. Enter it at the
masked prompt; never use `--body`, a command-line value, a committed parameter
file, or task evidence:

```powershell
gh secret set BUDGET_ALERT_CONTACT_EMAIL --env $environment --repo $repo
```

Do not create `AZURE_CLIENT_SECRET`. Do not add `alpaca-api-key`,
`alpaca-api-secret`, or `api-bearer-token` to GitHub.

The repository variable is a single selector. If both environments exist,
changing it to `prod` means subsequent matching pushes deploy prod, not dev.
Treat the selector change as a reviewed production change and verify it before
merging. The current workflow cannot deploy dev and prod independently from
one push and has no `workflow_dispatch` input.

## Configure Package Access

The workflows use the run-scoped `GITHUB_TOKEN`; no PAT or registry password
belongs in GitHub secrets.

- **Settings > Actions > General > Workflow permissions** may remain at the
  current read-only default. Job-level permissions explicitly grant only what
  each job needs.
- `publish-image` requires `contents: read` and `packages: write` to publish
  `ghcr.io/Carson-Fricke/FCA:<commit-sha>`.
- `deploy` requires `contents: read`, `id-token: write`, and `packages: read`.
  It imports the private GHCR digest into private ACR using only the masked
  run-scoped token.
- In the private `fca` container package's settings, retain inherited access
  from this repository or add `Carson-Fricke/FCA` under **Manage Actions
  access** with read/write access sufficient for the two jobs. Do not make the
  package public merely to fix an import.

The successful hosted dev runs prove the current private package permissions.
If import later returns unauthorized, inspect package Actions access and the
job's `packages: read`; do not mint a long-lived GHCR credential.

## First Full Deployment And Vault Creation

`infra/bootstrap-github-oidc.bicep` intentionally does not create Key Vault or
runtime secrets. `infra/main.bicep` creates the vault and runtime
`Key Vault Secrets User` assignment, while also declaring versionless secret
references on the workloads.

For existing dev, proceed directly to secret seeding or rotation. For a new
environment, use this staging sequence:

1. Select the Environment in `DEPLOYMENT_ENVIRONMENT` and verify all eleven
   base Environment variables, the quant-runtime variables listed under
   "Dev Deployment Enablement And Rebootstrap", and the budget-contact secret.
2. Merge a reviewed, path-matching change to `master`. Alternatively, rerun an
   explicitly approved prior `deploy-main.yml` run only when deploying that
   exact commit is intended. There is no supported manual-dispatch input.
3. The first run can stop when Container Apps resolves not-yet-created secret
   versions. The incremental deployment should already have created the vault
   and runtime vault role. This staging failure is not a completed deployment.
4. Discover the vault by environment tag, grant the human seeder access, and
   seed all three names as described below.
5. Wait for RBAC and Key Vault reference propagation, then rerun all jobs for
   the same approved commit or allow the next reviewed matching push to deploy.
6. Require OIDC login, full Bicep deployment, digest verification, and the
   public or private health smoke to pass before accepting the environment.

Discover the vault rather than assuming a generated prod name:

```powershell
$vaultQuery = "[?tags.application=='fca' && " +
  "tags.product=='market-lake' && " +
  "tags.environment=='$environment'].name | [0]"
$vault = az keyvault list `
  --resource-group $resourceGroup `
  --query $vaultQuery `
  --output tsv
if (-not $vault) {
  throw 'The main deployment has not created the environment vault'
}
```

An RBAC-authorized operator can grant the signed-in human the least-privilege
seeding role. This is a data-plane role; `Owner` or `Contributor` alone does
not grant secret access.

```powershell
$vaultId = az keyvault show `
  --name $vault `
  --query id `
  --output tsv
$maintainerObjectId = az ad signed-in-user show --query id --output tsv

az role assignment create `
  --assignee-object-id $maintainerObjectId `
  --assignee-principal-type User `
  --role 'Key Vault Secrets Officer' `
  --scope $vaultId `
  --only-show-errors `
  --output none
```

Allow several minutes, and up to 30 minutes, before treating a new role
assignment as broken. When public network access is disabled, run data-plane
commands only from an approved host with private endpoint and DNS reachability.

## Seed Runtime Secrets

The exact Key Vault names and consumers are:

| Key Vault secret | Runtime environment variable | Consumers |
| --- | --- | --- |
| `alpaca-api-key` | `LAKE_PROVIDER__API_KEY` | API and ingest job |
| `alpaca-api-secret` | `LAKE_PROVIDER__API_SECRET` | API and ingest job |
| `api-bearer-token` | `LAKE_API__BEARER_TOKEN` | API only |

Use the appropriate Alpaca paper/data credentials for dev and separately
approved account credentials for prod. The API bearer token is application
owned, not an Alpaca credential.

Azure CLI cannot read a secret value from standard input. This helper uses a
masked prompt and a short-lived UTF-8 file so the value never appears in shell
history, process arguments, command output, or the repository:

```powershell
function Set-MaskedVaultSecret([string] $Name) {
  $secure = Read-Host "Enter $Name" -AsSecureString
  $bstr = [IntPtr]::Zero
  $path = Join-Path ([IO.Path]::GetTempPath()) ([IO.Path]::GetRandomFileName())
  try {
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    [IO.File]::WriteAllText(
      $path,
      $plain,
      (New-Object Text.UTF8Encoding($false))
    )
    $plain = $null
    az keyvault secret set `
      --vault-name $vault `
      --name $Name `
      --file $path `
      --encoding utf-8 `
      --only-show-errors `
      --output none
    if ($LASTEXITCODE -ne 0) { throw "Failed to set $Name" }
  } finally {
    $plain = $null
    if ($bstr -ne [IntPtr]::Zero) {
      [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
    Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
  }
}

Set-MaskedVaultSecret 'alpaca-api-key'
Set-MaskedVaultSecret 'alpaca-api-secret'
```

Generate a 256-bit application token without printing it:

```powershell
function Set-GeneratedBearerToken {
  $bytes = New-Object byte[] 32
  $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
  $path = Join-Path ([IO.Path]::GetTempPath()) ([IO.Path]::GetRandomFileName())
  try {
    $rng.GetBytes($bytes)
    $token = [Convert]::ToBase64String($bytes)
    $token = $token.TrimEnd('=').Replace('+', '-').Replace('/', '_')
    [IO.File]::WriteAllText(
      $path,
      $token,
      (New-Object Text.UTF8Encoding($false))
    )
    $token = $null
    az keyvault secret set `
      --vault-name $vault `
      --name api-bearer-token `
      --file $path `
      --encoding utf-8 `
      --only-show-errors `
      --output none
    if ($LASTEXITCODE -ne 0) { throw 'Failed to set api-bearer-token' }
  } finally {
    $token = $null
    $rng.Dispose()
    [Array]::Clear($bytes, 0, $bytes.Length)
    Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
  }
}

Set-GeneratedBearerToken
```

If clients already use an approved bearer token, seed that token with
`Set-MaskedVaultSecret 'api-bearer-token'` instead. Transfer bearer tokens to
clients only through their approved secret manager, never through logs, chat,
issues, or task evidence.

Confirm names and enabled state without reading values:

```powershell
$secretNamesQuery =
  "[?name=='alpaca-api-key' || name=='alpaca-api-secret' || " +
  "name=='api-bearer-token'].{name:name,enabled:attributes.enabled}"
az keyvault secret list `
  --vault-name $vault `
  --query $secretNamesQuery `
  --output table
```

## Rotate And Prove Current Versions

Container Apps uses versionless Key Vault URLs. A new enabled version becomes
the current value; Azure refreshes versionless references within about 30
minutes and restarts active revisions that consume a changed value. Rotation
is not atomic across the Alpaca key and secret, so use an Alpaca overlap window
when available.

1. Create or obtain the replacement Alpaca credential pair, but retain the old
   pair until validation succeeds.
2. Run `Set-MaskedVaultSecret` for both Alpaca names. Each call creates a new
   Key Vault version.
3. Coordinate API clients, then seed or generate a new `api-bearer-token`.
   The application accepts only one token, so this rotation can interrupt
   clients that have not switched.
4. Record only version IDs, creation times, and enabled state. Never record
   values.
5. Wait for automatic refresh or restart the active API revision, then start a
   new ingest execution after the rotation time.
6. Require the API revision to become healthy. From an approved caller, require
   the new token to pass `/v1/auth/check` and the old token to return 401.
7. Require the post-rotation ingest execution to succeed against Alpaca before
   revoking the old Alpaca credential.

Safe version metadata and immediate refresh commands:

```powershell
foreach ($name in @('alpaca-api-key', 'alpaca-api-secret', 'api-bearer-token')) {
  az keyvault secret list-versions `
    --vault-name $vault `
    --name $name `
    --query "[].{id:id,created:attributes.created,enabled:attributes.enabled}" `
    --output table
}

$normalizedPrefix = $resourceNamePrefix.ToLower()
$normalizedEnvironment = $environment.ToLower()
$apiApp = "$normalizedPrefix-$normalizedEnvironment-api"
$ingestJob = "$normalizedPrefix-$normalizedEnvironment-ingest"

$activeRevision = az containerapp revision list `
  --name $apiApp `
  --resource-group $resourceGroup `
  --query "[?properties.active].name | [0]" `
  --output tsv
az containerapp revision restart `
  --name $apiApp `
  --resource-group $resourceGroup `
  --revision $activeRevision `
  --only-show-errors `
  --output none

$executionName = az containerapp job start `
  --name $ingestJob `
  --resource-group $resourceGroup `
  --only-show-errors `
  --query name `
  --output tsv
if (-not $executionName) { throw 'Ingest validation did not start' }

# Poll that exact execution until it reaches a terminal state.
$executionStatus = 'Running'
for ($attempt = 0; $attempt -lt 60; $attempt++) {
  $executionStatus = az containerapp job execution list `
    --name $ingestJob `
    --resource-group $resourceGroup `
    --query "[?name=='$executionName'].properties.status | [0]" `
    --output tsv
  if ($executionStatus -in @('Succeeded', 'Failed')) { break }
  Start-Sleep -Seconds 10
}
if ($executionStatus -ne 'Succeeded') {
  throw "Ingest validation did not succeed: $executionStatus"
}

# Run from a network path that can reach the API ingress.
$apiFqdn = az containerapp show `
  --name $apiApp `
  --resource-group $resourceGroup `
  --query properties.configuration.ingress.fqdn `
  --output tsv
$bearer = az keyvault secret show `
  --vault-name $vault `
  --name api-bearer-token `
  --query value `
  --output tsv
try {
  $headers = @{ Authorization = "Bearer $bearer" }
  $auth = Invoke-RestMethod `
    -Uri "https://$apiFqdn/v1/auth/check" `
    -Headers $headers
  if ($auth.status -ne 'ok') { throw 'Bearer validation failed' }
} finally {
  if ($headers) { $headers.Clear() }
  $headers = $null
  $bearer = $null
}
```

An API revision that starts and passes all three `/health` probes proves that
all three Key Vault references can be resolved. A new successful ingest
execution proves that the ingest job can resolve the current Alpaca pair and
authenticate to the provider. A protected-route call is still required to
prove the new bearer value itself. For private ingress, make that call only
from an approved network path; do not temporarily expose the API for testing.

For rollback, re-enter the previous approved value as a new latest version,
then repeat the refresh and validation. Do not assume that disabling the newest
version makes a versionless URL fall back to an older enabled version. Keep old
Alpaca credentials valid until rollback is no longer needed.

## Validate The Bootstrap

These checks disclose identifiers, names, and states only.

```powershell
# GitHub immutable subject mode and selected environment.
gh api "repos/$repo/actions/oidc/customization/sub"
gh api "repos/$repo/actions/variables/DEPLOYMENT_ENVIRONMENT"
gh api "repos/$repo/environments/$environment"
gh api "repos/$repo/environments/$environment/deployment-branch-policies"
gh api "repos/$repo/environments/$environment/variables?per_page=100" `
  --jq '.variables | sort_by(.name) | .[] | [.name,.value] | @tsv'
gh api "repos/$repo/environments/$environment/secrets" `
  --jq '.secrets[].name'

# Azure credential and role assignment metadata, including child scopes.
$deployer = az identity show `
  --resource-group $resourceGroup `
  --name $identityName `
  --query '{clientId:clientId,principalId:principalId,id:id}' `
  --output json | ConvertFrom-Json

az identity federated-credential show `
  --resource-group $resourceGroup `
  --identity-name $identityName `
  --name github-deploy `
  --query '{issuer:issuer,audiences:audiences,subject:subject}' `
  --output json

$runtimeIdentityName =
  "$($resourceNamePrefix.ToLower())-$($environment.ToLower())-runtime"
$runtime = az identity show `
  --resource-group $resourceGroup `
  --name $runtimeIdentityName `
  --query '{clientId:clientId,principalId:principalId,id:id}' `
  --output json | ConvertFrom-Json
$resourceGroupId = az group show `
  --name $resourceGroup `
  --query id `
  --output tsv
$scopedRolesQuery =
  "[?starts_with(scope, '$resourceGroupId')]." +
  "{role:roleDefinitionName,scope:scope,condition:condition}"

az role assignment list `
  --assignee $deployer.principalId `
  --all `
  --query $scopedRolesQuery `
  --output table
az role assignment list `
  --assignee $runtime.principalId `
  --all `
  --query $scopedRolesQuery `
  --output table

# Secret-reference metadata, never secret values.
az keyvault secret list `
  --vault-name $vault `
  --query "[].{name:name,enabled:attributes.enabled}" `
  --output table
$secretReferenceQuery =
  '{state:properties.provisioningState,' +
  'image:properties.template.containers[0].image,' +
  'secrets:properties.configuration.secrets[].{' +
  'name:name,keyVaultUrl:keyVaultUrl,identity:identity}}'
az containerapp show `
  --name $apiApp `
  --resource-group $resourceGroup `
  --query $secretReferenceQuery `
  --output json
az containerapp job show `
  --name $ingestJob `
  --resource-group $resourceGroup `
  --query $secretReferenceQuery `
  --output json

# Hosted deployment status. Avoid --log unless diagnosing a specific failure.
gh run list `
  --workflow deploy-main.yml `
  --limit 5 `
  --json databaseId,headSha,status,conclusion,url
```

Acceptance requires all of the following:

- GitHub reports the immutable default prefix and Azure matches the exact
  environment subject, issuer, and audience.
- The Environment has all eleven base non-secret values, the quant-runtime
  variables, the branch policy, the budget-contact secret name, and any
  plan-supported reviewer protection.
- The package remains private and no PAT, Azure client secret, provider value,
  or bearer value exists in GitHub.
- The runtime identity has `Key Vault Secrets User` only at its vault and the
  expected storage and ACR data roles at their resource scopes.
- API references all three versionless vault URLs; ingest references only the
  two Alpaca URLs. Both use the runtime identity.
- A hosted run passes OIDC login, immutable import, Bicep deployment, digest
  checks, and the appropriate health smoke.
- A controlled post-seed or post-rotation ingest succeeds without printing a
  provider response or credential.

## Recurring Deployment

After bootstrap, a push to `master` runs only when its changed paths match
`deploy-main.yml`:

1. `publish-image` writes a commit-tagged private GHCR image and returns its
   immutable digest.
2. The deploy job targets `vars.DEPLOYMENT_ENVIRONMENT`; GitHub evaluates that
   Environment's available protection rules before issuing its OIDC token.
3. `azure/login` exchanges the environment-scoped token using client, tenant,
   and subscription IDs. There is no client secret.
4. The job imports the exact private GHCR digest into private ACR with its
   masked run token and verifies that the digest did not change.
5. It validates and incrementally deploys `infra/main.bicep` with
   `assignGithubDeployerRoles=false`.
6. It verifies the API and ingest job use the same ACR digest and runtime
   managed identity.
7. Public ingress receives a direct `/health` request. Private ingress is
   temporarily scaled to one replica, must pass all three `/health` probes,
   and is restored to zero minimum replicas through an exit trap.

Review `DEPLOYMENT_ENVIRONMENT`, `BUDGET_START_DATE`, network posture, cron,
and timeout before each promotion. Preserve an already accepted budget start
date unless intentionally rebasing the budget period. Do not create a no-op
commit solely to deploy, and do not enable `assignGithubDeployerRoles` in a
recurring run.

## Dev Deployment Enablement And Rebootstrap

The 2026-09-07 deploy that landed the quant-harness runtime wave required a
one-time enablement pass. Record it here because the recurring pipeline can
hit identical failures after future waves change the authorization surface.

Required `dev` Environment variables for `deploy-main.yml` input validation:
`ENABLE_CRYPTO_TRADER`, `RTH_TRADER_CREDENTIAL_MODE`,
`CRYPTO_TRADER_CREDENTIAL_MODE`, `RTH_SCHEDULE_TIMEZONE`, `RTH_SCHEDULE_START`,
`RTH_SCHEDULE_END`, `RTH_TRADER_REPLICAS`, and
`BACKTEST_REPLICA_TIMEOUT_SECONDS`. The schedule values are Container Apps cron
expressions (`30 9 * * 1-5` and `0 16 * * 1-5` in `America/New_York`), not
clock times, and must satisfy the Bicep `minLength` of 9. Current dev values:
crypto trader disabled, paper credential mode, one RTH replica, 3600-second
backtest timeout.

When a wave adds role assignments for a new runtime principal or a new
role-assignment scope (such as the dedicated Thesis workspace storage account),
the deployer's conditioned RBAC Administrator grant must cover it or the
deployment fails with `roleAssignments/write` authorization errors at that
scope. The `rebootstrapWhen` rule in `access.bicep` names this condition. The
grant is a single resource-group-scoped Role Based Access Control Administrator
assignment whose condition allows only the allow-listed data roles
(`AcrPull`, Storage Blob/Queue/Table, and `Key Vault Secrets User`) for any
workload principal; the resource-group scope is what covers new accounts. Its
resource name matches the standalone bootstrap, so re-running either
`bootstrap-github-oidc.bicep` or `main.bicep` with
`assignGithubDeployerRoles=true` updates the same assignment. Because the
condition is baked into the deployed role assignment, a code merge alone cannot
refresh it: an authorized maintainer must deploy `main.bicep` once from an
Owner identity with `assignGithubDeployerRoles=true`, then never again;
recurring runs keep the flag `false`. The quant-runtime `AcrPull` assignment is
still not templated anywhere, but the resource-group scope plus the `AcrPull`
role in the condition now lets the rebootstrap create it instead of a separate
manual step. Recreate it the same way only if the registry or the quant-runtime
identity is rebuilt.

The KEDA cron custom scaler requires `desiredReplicas` metadata and rejects
unknown metadata keys, so `container-apps.bicep` sets `desiredReplicas` and no
`value`. Traders read the single `alpaca-api-key`/`alpaca-api-secret` pair in
both paper and live credential modes because Alpaca issues one key pair across
endpoints; the mode exists for future endpoint selection only.

Current dev API posture after this pass: `ENABLE_EXTERNAL_API_INGRESS=true`
and no `LAKE_API_AUDIENCE` variable, so the lake API is publicly reachable and
authenticates with the static `api-bearer-token`. This restores local soak
tooling access. Re-enabling Entra audience mode (T180) requires the soak
driver to accept a credential plus audience and the calling principal to hold
the `Lake.Access` application role; do not set `LAKE_API_AUDIENCE` before both
exist.

## Recovery And Rollback

- Empty `AZURE_CLIENT_ID`: confirm the repository selector exists and every
  Azure value is on the exact selected Environment, not another scope.
- `AADSTS700213`: compare the Azure subject with GitHub's current immutable
  prefix and exact Environment name, including casing and `%3A` encoding.
- Provider-not-registered: register the missing namespace as a subscription
  operator and wait for `Registered`; do not grant subscription rights to the
  GitHub deployer.
- Authorization failure after bootstrap: wait for RBAC propagation, then
  inspect Contributor and the conditioned RBAC administrator assignment. Do
  not add `Owner`.
- Vault 403: distinguish RBAC propagation from networking. Confirm the human
  has `Key Vault Secrets Officer`, the runtime has `Key Vault Secrets User`,
  and the caller can reach the public or private endpoint.
- Secret-reference failure: confirm names, enabled current versions,
  versionless URLs, runtime identity attachment, and vault network access.
- Package import failure: confirm `packages: read`, private package Actions
  access for this repository, and that the exact source digest exists.
- Image pull failure: confirm the mirror digest exists, ACR admin remains
  disabled, and the runtime identity has `AcrPull`.
- Failed resource deployment: inspect the named resource-group deployment and
  rerun the same approved commit after correction. Bicep uses incremental mode
  and bootstrap resources are idempotent.
- Bad source or Bicep release: revert the bad commit on `master` and let the
  selected Environment deploy the resulting reviewed commit and digest. Check
  the repository selector first so rollback targets the intended environment.
- Bad secret version: re-seed the prior value as a new latest version, refresh
  the API, start ingest, and validate before revoking provider credentials.
- Emergency OIDC stop: delete only `github-deploy` from the environment's
  deployer identity. Running workloads continue, but new GitHub Azure sessions
  fail. Re-run the authorized bootstrap to restore the exact credential.
- Emergency authorization stop: remove the deployer's Contributor,
  conditioned RBAC administrator, and ACR assignment. Existing workloads keep
  running, but GitHub cannot update resources or import images.

Never print Environment secrets, Azure access tokens, Key Vault values,
provider responses containing account data, or bearer tokens while diagnosing
a run. Avoid `--debug`, shell tracing, PowerShell transcripts, unfiltered ARM
outputs, and `gh run view --log` around secret operations.
