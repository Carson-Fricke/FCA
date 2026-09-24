# Thesis Cosmos DB deployment

<!-- markdownlint-disable MD013 -->

This runbook covers the deployed Azure Cosmos DB account that backs Thesis
durable state: every chat, timeline item, execution document, and workflow
event. Local development uses the Cosmos DB emulator
(`.github/runbooks/thesis-local-stack.md`); Azure uses the account provisioned
by `infra/modules/thesis-cosmos.bicep` through the Thesis composition in
`infra/main.bicep`. No manual `az cosmosdb create` step exists and no account
key is ever created.

## 1. What the composition provisions

The `thesis-cosmos` module deploys only when `deployThesis` is true:

- One dedicated SQL API account named `<prefix>-<env>-cosmos-<suffix>`
  (globally unique, derived from `resourceGroup().id`).
- Serverless capacity: no provisioned RU/s and effectively $0 idle cost;
  requests bill per RU and storage bills per GB-month under the existing
  resource-group budget alerts.
- `disableLocalAuth: true`: the account has no keys. Authentication is the
  Thesis runtime user-assigned managed identity and nothing else.
- Database `thesis` and three containers, matching the closed set in
  `src/thesis/cosmos/client.py`:
  | Container | Partition key | Contents |
  | --- | --- | --- |
  | `timeline` | `/chatId` | Messages and execution events per chat |
  | `catalog` | `/workspaceId` | Chats, executions, workspace records |
  | `executions` | `/executionId` | One durable execution-store document per run |
- A Cosmos data-plane role assignment for the Thesis managed identity
  ("Cosmos DB Built-in Data Contributor", account scope). Data-plane RBAC is
  separate from Azure RBAC; no control-plane role is needed by the runtime.

## 2. Deploy

The Cosmos account is part of the normal Thesis deployment; no extra step:

```bash
az deployment group create \
  --resource-group <resource-group> \
  --parameters infra/parameters/thesis.dev.bicepparam
```

Or run the `deploy-thesis` workflow, which supplies the same parameters plus
the digest-pinned image reference. The `deploymentSummary.thesis.cosmos`
output reports the endpoint, database, containers, and role assignment id for
non-secret verification.

Two deployment properties matter:

1. **Schema ownership is Bicep's.** The module creates the database and
   containers, so a fresh container app only needs metadata reads. The
   runtime's `ensure_database()` remains an idempotent safety net for the
   emulator and for re-created containers, but a production deployment never
   depends on the app creating its own schema.
2. **Role-assignment propagation.** A new data-plane assignment can take 5-10
   minutes to become effective. The container app may restart with a 403 until
   it propagates; this is transient. Do not "fix" it by re-enabling local auth.

## 3. Verify the wiring

```bash
# The account exists and has local auth disabled.
az cosmosdb show -g <resource-group> -n <account> \
  --query "{endpoint:documentEndpoint, disableLocalAuth:disableLocalAuth}" -o json

# The runtime schema exists.
az cosmosdb sql database list -g <resource-group> -a <account> -o table
az cosmosdb sql container list -g <resource-group> -a <account> -d thesis -o table

# The data-plane role assignment names the Thesis identity.
az cosmosdb sql role assignment list -g <resource-group> -a <account> -o table

# The container app receives the endpoint and database, and no key.
az containerapp show -g <resource-group> -n <thesis-api> \
  --query "properties.template.containers[0].env[?starts_with(name, 'THESIS_COSMOS')]" -o json
```

Then exercise one write path end to end: send a chat message through the API
and confirm the `timeline` container gains the corresponding document (Data
Explorer, or `az cosmosdb sql query` with an Entra-authenticated token). A
healthy deployment shows the two `THESIS_COSMOS_*` variables and zero
`THESIS_COSMOS_KEY`.

## 4. How the runtime consumes it

- `infra/modules/thesis.bicep` injects `THESIS_COSMOS_ENDPOINT` and
  `THESIS_COSMOS_DATABASE` (both non-secret) and the container app's
  `AZURE_CLIENT_ID`.
- `thesis serve` composes the app through
  `serving_app_from_environment()` -> `open_cosmos_chat_stack()`, which reads
  those variables with `CosmosSettings.from_environment()`.
- Without `THESIS_COSMOS_KEY`, `open_cosmos_client()` authenticates with
  `DefaultAzureCredential`, which resolves the user-assigned managed identity
  through `AZURE_CLIENT_ID` and presents the data-plane role above. A loopback
  endpoint without a key still uses the public emulator key, so local
  development is unchanged.
- `open_execution_store()` reads the same settings, so the `executions`
  container serves the workflow execution store wherever that runtime is
  composed.

## 5. Workflow runtime wiring

`thesis serve` composes the conversations service, control tools, and the
workflow runtime only when it can authenticate the model and the harness
(`thesis/http/serving.py`). The deployment injects:

| Variable | Source | Purpose |
| --- | --- | --- |
| `THESIS_MODEL` | non-secret parameter | OpenRouter model name; empty uses the runtime default |
| `OPENROUTER_API_KEY` | Key Vault secret (operator-provisioned) | model replies and every phase agent |
| `HARNESS_MCP_URL` | derived by `main.bicep` from the internal market-lake api endpoint | harness tools and studies |
| `HARNESS_API_BEARER_TOKEN` | Key Vault secret `harness-api-bearer-token` (already required by the market-lake api app) | harness authentication |

Enable model replies and workflows once:

```bash
az keyvault secret set --vault-name <vault> --name thesis-openrouter-api-key --value <key>
```

then set `thesisModelApiKeySecretName = 'thesis-openrouter-api-key'` in the
parameter file and redeploy. Until then the app still serves chat, timeline,
and workspaces; the workflow launch surface reports no workflow available
rather than failing startup.

Two limits are deliberate until the Azure workspace provider lands (T343/T344):
research-model publications, indicator definitions, and hypothesis artifacts
stay in process memory when no workspace tree is bound; runs, trials, and
conversations remain durable in the execution store and Cosmos.

## 6. Troubleshooting

- **403 Forbidden from the Cosmos SDK at startup**: the role assignment has
  not propagated yet, or `thesisPrincipalId` is not the Thesis identity's
  object id. Verify the assignment with
  `az cosmosdb sql role assignment list`; restart the revision after the
  propagation window rather than enabling keys.
- **The app still talks to localhost:8081 in Azure**: the `THESIS_COSMOS_*`
  variables are missing from the revision. Check
  `az containerapp show ... env` and redeploy; the image itself has no
  environment branch and defaults to the emulator.
- **Schema drift**: a container's partition key must match
  `src/thesis/cosmos/client.py`. Changing one without the other breaks point
  reads; update both in the same change.
- **Name collision**: Cosmos account names are globally unique. Re-run the
  deployment in the target resource group (the deterministic suffix includes
  the group id) or choose a new prefix.

## 7. Key-based fallback (exceptional)

The default is identity-only; keys are never provisioned. If a legacy tool
requires key auth, that is a deliberate change requiring all of:

1. A parameter to set `disableLocalAuth: false` on the account.
2. An operator-provisioned Key Vault secret plus a `THESIS_COSMOS_KEY`
   secretRef in `infra/modules/thesis.bicep`.
3. A rotation plan, because the account key becomes a durable credential.

Record the decision before doing it; the identity path has no secret to leak
and needs no rotation.
