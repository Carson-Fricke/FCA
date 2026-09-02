# Key Vault Runtime Secrets

The market-lake API and ingest job read provider credentials from Azure Key
Vault through the runtime user-assigned managed identity. No secret value is a
Bicep parameter, GitHub variable, repository file, or task-evidence field.

## Secret Names

| Key Vault secret | Workload environment variable | Consumers |
| --- | --- | --- |
| `alpaca-api-key` | `LAKE_PROVIDER__API_KEY` | API and ingest job |
| `alpaca-api-secret` | `LAKE_PROVIDER__API_SECRET` | API and ingest job |
| `api-bearer-token` | `LAKE_API__BEARER_TOKEN` | API only |

Container Apps uses versionless Key Vault URLs, so a new enabled secret version
is selected automatically. Azure refreshes versionless references within about
30 minutes and restarts active revisions that consume the changed value.

## Required Access

- Runtime identity `fca-ml-dev-runtime` requires `Key Vault Secrets User` on
  this vault only. It must not receive secret write permissions.
- The maintainer seeding or rotating values requires `Key Vault Secrets
  Officer` on this vault, or an equivalent authorized Azure data-plane role.
- Dev currently enables the Key Vault public endpoint because no private
  endpoint or private DNS module exists. Microsoft Entra authentication and
  vault-scoped RBAC remain mandatory.
- Allow several minutes for a new RBAC assignment to propagate before seeding.

## Seed Or Rotate

Use an interactive shell so values are never arguments, shell history, or log
output. This PowerShell helper uses a masked prompt and a short-lived UTF-8
file because Azure CLI supports `--file` but not secret input on stdin:

```powershell
$vault = 'fca-ml-dev-kv-obj3kww'
function Set-MaskedVaultSecret([string] $name) {
  $secure = Read-Host "Enter $name" -AsSecureString
  $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
  $path = [IO.Path]::GetTempFileName()
  try {
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    [IO.File]::WriteAllText($path, $plain, (New-Object Text.UTF8Encoding($false)))
    az keyvault secret set --vault-name $vault --name $name --file $path --encoding utf-8 --output none
    if (-not $?) { throw "Failed to set $name" }
  } finally {
    $plain = $null
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
  }
}

Set-MaskedVaultSecret alpaca-api-key
Set-MaskedVaultSecret alpaca-api-secret

# The bearer token belongs to this application, not to Alpaca. Generate it
# locally when no existing client token has been provisioned.
function Set-GeneratedVaultSecret([string] $name) {
  $bytes = New-Object byte[] 32
  $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
  $path = [IO.Path]::GetTempFileName()
  try {
    $rng.GetBytes($bytes)
    $token = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
    [IO.File]::WriteAllText($path, $token, (New-Object Text.UTF8Encoding($false)))
    az keyvault secret set --vault-name $vault --name $name --file $path --encoding utf-8 --output none
    if (-not $?) { throw "Failed to set $name" }
  } finally {
    $token = $null
    $rng.Dispose()
    [Array]::Clear($bytes, 0, $bytes.Length)
    Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
  }
}

Set-GeneratedVaultSecret api-bearer-token
```

Never paste values into this runbook, a GitHub variable, Bicep parameter,
issue, or task evidence.

Confirm only names and enabled state, never values:

```powershell
az keyvault secret list `
  --vault-name fca-ml-dev-kv-obj3kww `
  --query "[].{name:name, enabled:attributes.enabled}" `
  --output table
```

Retrieve the generated application token only when you need to call a
protected API route, and keep it out of shell history and logs:

```powershell
az keyvault secret show `
  --vault-name fca-ml-dev-kv-obj3kww `
  --name api-bearer-token `
  --query value `
  --output tsv
```

## Deploy And Verify

After all three names exist, deploy the Bicep composition. Confirm that:

- API secrets reference all three versionless Key Vault URLs.
- Ingest secrets reference only the two Alpaca values.
- Both workloads identify `fca-ml-dev-runtime` for Key Vault retrieval.
- Environment variables use `secretRef`; no resolved value appears in ARM,
  Container Apps configuration output, GitHub logs, or task evidence.

Run a bounded provider request and ingest only after references report healthy.
Record status, object path, row count, and timing, but never provider responses
that may contain account information or any credential value.

## Rollback

- Secret rotation: disable the bad version and re-enable the previous version,
  then restart the affected revision or wait for automatic refresh.
- Workload rollback: deploy the previous image or Bicep revision. Do not delete
  a secret version while an active revision still references it.
- Reference failure: verify vault network access, enabled secret versions,
  runtime identity attachment, and `Key Vault Secrets User`; allow for RBAC and
  reference-refresh delay before changing roles.
- Emergency access removal: remove the runtime vault role assignment. This
  blocks future secret resolution and restarts, but does not erase values.
