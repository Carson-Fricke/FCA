# Entra Application Role Bootstrap for Platform APIs

Managed-identity-to-API authentication for FCA platform APIs uses Entra ID
application role assignments instead of shared bearer secrets. A caller
managed identity mints an access token whose audience is the target API
application and whose `roles` claim contains the assigned role; the API
validates the token and enforces the role claim.

This is a one-time, tenant-scoped bootstrap performed by an authorized Entra
administrator. It is intentionally not part of the resource-group Bicep
composition: Entra objects are tenant-scoped, and the GitHub deployer identity
is deliberately least-privileged (resource-group Contributor plus conditioned
RBAC roles only), so it must not receive Graph rights to create or modify
applications.

## Platform API Applications

| Application | Client ID (token audience) | App role | Role definition ID |
| --- | --- | --- | --- |
| `fca-ml-lake-api` | `cbc572d2-ae2e-4501-af76-0515e2f73bcd` | `Lake.Access` | `ac1bdec8-98c6-4060-afb1-a9fab63d1a93` |
| `fca-harness-api` | `c380c885-4a51-4b0d-9dc7-2f0c2fc70705` | `Harness.Access` | `b5b73fca-7efc-427d-9fcd-2dc5b482077b` |

Both applications are single-tenant (`AzureADMyOrg`) with one application
role whose allowed member type is `Application` (service principals and
managed identities only). `Harness.Access` enforcement begins when the
harness runtime API (backtest query and job launch) is deployed.

## Existing Assignments

| Caller identity | Principal ID | Role | Application |
| --- | --- | --- | --- |
| `fca-ml-dev-runtime` (user-assigned managed identity shared by the API Container App and ingest Container Apps Job) | `550d9d36-f59c-4af9-b228-71d1f51d0e9f` | `Lake.Access` | `fca-ml-lake-api` |

A hosted verification job minted a token from this identity and confirmed the
claims: `aud` equals the lake application client ID, `roles` contains
`Lake.Access`, and `appid` identifies the caller managed identity.

## Token Contract the APIs Enforce

- Signature: validated against the tenant JWKS discovery document.
- Audience: exactly the target application client ID.
- Issuer: managed identities mint v1 tokens with
  `https://sts.windows.net/<tenant-id>/`; validators must also accept the v2
  form `https://login.microsoftonline.com/<tenant-id>/v2.0`.
- Expiry: standard Entra lifetimes (about 60 to 90 minutes); clients re-mint
  silently through `DefaultAzureCredential`.
- Authorization: the role claim must contain the role value for the target
  API. No shared secret is involved.

## Bootstrap Commands

Run once per tenant by an authorized administrator. Generate a new GUID for
each role definition.

```powershell
$roleId = [guid]::NewGuid().ToString()
$rolesJson = '[{"allowedMemberTypes":["Application"],"displayName":"Lake API Access","description":"Caller may access the market-lake API data plane on behalf of its managed identity.","isEnabled":true,"value":"Lake.Access","id":"' + $roleId + '"}]'
Set-Content -Path "$env:TEMP\lake-app-roles.json" -Value $rolesJson -Encoding UTF8
$app = az ad app create --display-name fca-ml-lake-api --sign-in-audience AzureADMyOrg --app-roles "@$env:TEMP\lake-app-roles.json" --output json | ConvertFrom-Json
$sp = az ad sp create --id $app.appId --output json | ConvertFrom-Json
```

Assign a workload managed identity (repeat per caller):

```powershell
$body = '{"principalId":"<caller-sp-object-id>","resourceId":"<api-sp-object-id>","appRoleId":"<role-definition-id>"}'
Set-Content -Path "$env:TEMP\role-assign.json" -Value $body -Encoding UTF8
az rest --method POST --url "https://graph.microsoft.com/v1.0/servicePrincipals/<caller-sp-object-id>/appRoleAssignments" --headers "Content-Type=application/json" --body "@$env:TEMP\role-assign.json"
```

Caller SP object IDs: for user-assigned managed identities the principal ID
shown by `az identity show` is the service principal object ID.

## Assigning Future Workload Identities

- Harness runtime workloads (trader, backtest API) receive their own managed
  identities when the quant runtime Bicep composition lands. Each identity
  that calls a platform API is assigned the matching role:
  `Lake.Access` for lake data access, `Harness.Access` for harness runtime
  API access.
- Cross-product access is the same pattern: a harness identity that reads the
  lake gets `Lake.Access`; a lake-side component that calls the harness query
  API would get `Harness.Access`.

## Security Notes

- Managed identity tokens can only be minted by workloads running on Azure
  infrastructure bound to the identity; the token cannot be lifted and replayed
  elsewhere.
- The role assignment is the authorization boundary; any identity in the
  tenant without it can mint a token for the audience but the API rejects it.
- No client secrets or certificates exist for these applications; there is
  nothing to rotate.
- The applications create no resource-group resources and appear in no Bicep
  deployment; this runbook is their record of truth.

## API Enforcement Configuration

The lake API enforces the token contract above through non-secret environment
configuration. Bicep injects both variables into the API container only when an
audience is configured (they arrive together or not at all):

- `LAKE_API__AUTH__ENTRA_TENANT_ID`: the deployment tenant ID
  (`6c199962-2933-4a4b-901f-46f1578826c4`), passed from `tenant().tenantId`.
- `LAKE_API__AUTH__ENTRA_AUDIENCE`: the `fca-ml-lake-api` application client
  ID `cbc572d2-ae2e-4501-af76-0515e2f73bcd`, the value tokens must name as
  their audience.

The enforced role value defaults to `Lake.Access` in
`config/market-lake/settings.yaml` (`api.auth.entra_role`). An empty or absent
audience keeps the API on static-bearer-only authentication, which remains the
local development path; the shared bearer secret retires once every hosted
caller authenticates with its managed identity. Clients mint tokens for the
audience with an `azure-identity` `TokenCredential` such as
`DefaultAzureCredential` (`lakeclient.LakeClient(credential=..., audience=...)`).
