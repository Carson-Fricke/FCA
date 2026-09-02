# GitHub Azure OIDC Bootstrap

This runbook separates the one-time authorized bootstrap from recurring
GitHub OIDC deployments. It creates no Azure client secret and stores no
provider credential or API bearer token in GitHub.

## Prerequisites

- An existing Azure resource group in the intended subscription and region.
- An Azure caller with `Contributor` and `Role Based Access Control
  Administrator` at that resource group, or `Owner` at an ancestor scope.
- Repository administration access for Actions variables, Environments, OIDC
  subject customization, and private package access.
- The `dev` Environment must restrict deployments to `master`. Add a required
  reviewer when the repository plan and visibility support that rule.
- Budget approval for one Basic Azure Container Registry. The private ACR is a
  runtime mirror; deleting it stops new revisions from pulling images.

## One-Time Azure Bootstrap

Run the bootstrap as the authorized human caller. The GitHub owner and
repository IDs are public identifiers, not credentials.

```powershell
az deployment group create `
  --name fca-github-oidc-bootstrap `
  --resource-group fca-rg-westus2 `
  --template-file infra/bootstrap-github-oidc.bicep `
  --parameters `
    location=westus2 `
    resourceNamePrefix=fca-ml `
    environmentName=dev `
    githubOwner=Carson-Fricke `
    githubOwnerId=48462838 `
    githubRepository=FCA `
    githubRepositoryId=1347935926
```

The deployment creates or confirms:

- `fca-ml-dev-runtime`, the workload user-assigned managed identity.
- `fca-ml-dev-github-deployer`, the separate deployment identity.
- `github-deploy`, whose subject is exactly
  `repo:Carson-Fricke@48462838/FCA@1347935926:environment:dev`.
- A deterministic private Basic ACR with its admin account disabled.
- `AcrPull` on that ACR for both the runtime and deployer identities.
- `Contributor` for the deployer at the dev resource group only.
- Conditioned `Role Based Access Control Administrator` at that resource group.
  Its condition permits only ACR pull, Blob, Queue, Table, and Key Vault secret
  data roles, and only for the runtime identity.

Treat deployment output as configuration data. Do not paste the full output
into issues or logs. Record only the client, tenant, subscription, resource
group, and subject values in their destinations below.

## GitHub Configuration

In **Settings > Secrets and variables > Actions**, create repository variable
`DEPLOYMENT_ENVIRONMENT=dev`. In **Settings > Environments**, create `dev`,
restrict deployment branches to `master`, and configure the available approval
rule. Set these Environment variables:

For this private personal repository, GitHub rejected the required-reviewer
rule because the current plan does not support it. The enforced protection is
therefore the custom `master`-only deployment branch policy. If the repository
becomes public or moves to a plan that supports reviewers for private
repositories, add the required-reviewer rule and enable prevent-self-review.

| Variable | Dev value |
| --- | --- |
| `AZURE_CLIENT_ID` | `githubConfiguration.azureClientId` bootstrap output |
| `AZURE_TENANT_ID` | `githubConfiguration.azureTenantId` bootstrap output |
| `AZURE_SUBSCRIPTION_ID` | `githubConfiguration.azureSubscriptionId` bootstrap output |
| `AZURE_RESOURCE_GROUP` | `fca-rg-westus2` |
| `AZURE_LOCATION` | `westus2` |
| `RESOURCE_NAME_PREFIX` | `fca-ml` |
| `BUDGET_START_DATE` | First day of the active budget month, such as `2026-09-01` |
| `ALLOW_PUBLIC_NETWORK_ACCESS` | `true` until private Key Vault connectivity exists |
| `ENABLE_EXTERNAL_API_INGRESS` | `false` for the approved dev posture |
| `INGEST_CRON_EXPRESSION` | `0 1 * * *` |
| `INGEST_REPLICA_TIMEOUT_SECONDS` | `3600` |

Add `BUDGET_ALERT_CONTACT_EMAIL` as an Environment secret, not a variable.
Enter it interactively with `gh secret set BUDGET_ALERT_CONTACT_EMAIL --env
dev --repo Carson-Fricke/FCA`, or use the GitHub settings page. Do not place the
value on a command line, in a parameter file committed to Git, or in task
evidence.

Keep the repository OIDC subject on GitHub's immutable default form. Confirm
`use_default=true`, `use_immutable_subject=true`, and the public
`sub_claim_prefix` through the repository Actions OIDC API. A custom claim-key
template will not match the Bicep federated credential.

## Recurring Deployment

After bootstrap, every push to `master` that matches the workflow paths:

1. Publishes an immutable digest-addressed GHCR image.
2. Uses `azure/login` with the Environment OIDC token and no client secret.
3. Imports the private GHCR digest into the private ACR with the run-scoped,
   masked `GITHUB_TOKEN`, then verifies the ACR manifest digest is unchanged.
4. Validates and creates `infra/main.bicep` with the immutable ACR digest and
   `assignGithubDeployerRoles=false`.
5. Confirms both workloads use the mirrored digest and pull with the runtime
   managed identity. No GHCR or ACR credential is stored in application state.
6. Calls `/health` directly for public ingress, or requires the private
   revision's three Azure HTTP probes against `/health` to report healthy.

Do not enable `assignGithubDeployerRoles` in recurring deployment. The
bootstrap owns the deployer authorization; the main deployment owns runtime
resources and runtime data-role assignments.

## Recovery And Rollback

- Empty `AZURE_CLIENT_ID`: confirm `DEPLOYMENT_ENVIRONMENT` is repository-scoped
  and all Azure values are variables on the selected Environment.
- OIDC `AADSTS700213`: compare the federated credential subject character for
  character with the immutable repository prefix and `environment:dev`; also
  confirm the workflow job targets `dev`.
- Authorization failure: wait several minutes for Azure RBAC propagation, then
  verify the deployer has resource-group Contributor and the conditioned RBAC
  administrator assignment. Do not add Owner to the deployer.
- Image import failure: confirm the deploy job has `packages: read`, the source
  digest exists in private GHCR, and the repository retains package access.
- Image pull failure: confirm the mirrored digest exists in ACR, the ACR admin
  account remains disabled, and the runtime identity has `AcrPull`.
- Failed resource deployment: inspect the named resource-group deployment and
  rerun the same commit after correction. Bicep is incremental and the
  bootstrap resources are idempotent.
- Federated credential rollback: restore the previous subject or delete only
  `github-deploy` from the deployer identity. Removing it immediately blocks
  new GitHub Azure sessions without affecting running workloads.
- Authorization rollback: remove the deployer's Contributor, conditioned RBAC
  administrator, and registry `AcrPull` assignments by their deployment
  outputs. Existing resources continue running, but GitHub cannot update or
  import images.

Never print Environment secrets, Azure access tokens, provider keys, or bearer
tokens while diagnosing a run.
