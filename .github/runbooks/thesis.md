# Thesis Operations Hardening

This runbook covers local and Azure Thesis operational safety: the shared
workspace/event/artifact/consent/audit boundaries, the documented recovery
procedures for interrupted durable execution, and the all-in-Git repository
size, backup, restore, and retention limits. It is written for the single
operator of the local release and the managed identity used in Azure mode.

## Runtime Modes And Equivalent Contracts

Local mode and Azure mode are first-class peers. Both bind each workspace to a
private Git repository and enforce the same durable contracts; only the host
and storage adapters differ. A mode split that weakens a boundary is a failure,
not a configuration choice.

| Contract | Local mode | Azure mode | Parity check |
| --- | --- | --- | --- |
| Workspace | Separate directory and Git repository under the operator root | Separate Git-backed entity with isolated execution and persistence scopes | `check_workspace_boundary` |
| Event | Durable local event store | Durable workspace event store | `check_event_boundary` |
| Artifact | Content-addressed path inside the workspace repository | Content-addressed path inside the workspace repository | `check_artifact_boundary` |
| Consent | Local consent grants and revocation ledger | Same consent contracts, same gatekeeper | `check_consent_boundary` |
| Audit | Residency audit records | Residency audit records | `check_audit_boundary` |

`thesis.operations.security.assert_local_azure_parity` compares the two
`ModeParityReport` values and refuses a split whose boundary outcomes differ.
`classify_provider_auth` reports provider authentication without ever trading
away offline workspace access.

## Security Boundaries

### Configure Azure workspace repositories

Local stack startup remains unchanged. Gitea settings alone do not switch a
local run to remote workspaces; Azure selection still requires
`THESIS_STORAGE_ACCOUNT_NAME`.

For an existing self-hosted forge, set `thesisGiteaUrl`,
`thesisGiteaOrganization`, and `thesisGiteaTokenSecretName` in the Azure
deployment. The token secret must already exist in Key Vault. Runtime
configuration is `THESIS_GITEA_URL`, `THESIS_GITEA_ORGANIZATION`, and
`THESIS_GITEA_TOKEN`. `THESIS_GITEA_CACHE_ROOT` optionally overrides the
rebuildable checkout cache. Partial Gitea configuration fails closed; all-empty
configuration preserves the existing Blob catalog until the forge is
bootstrapped. No token is embedded in clone URLs or persisted Git configuration.

To host the forge in this subscription, enable `enableGitea` and supply
`giteaImageReference` as an official Gitea image pinned by SHA-256 digest.
Provision the forge before enabling Thesis remote workspaces. From a shell
inside the Container Apps environment, bootstrap the administrator through
Gitea's admin CLI, create the configured organization, and create a service
token with repository and organization permissions needed to create private
repos. Store it in Key Vault; never paste it into a workspace manifest.
The organization must permit that service account to create repositories.
The module exposes only internal HTTPS ingress and disables public signup.

The initial host is single-replica SQLite on Azure Files SMB. Test filesystem
locking and crash recovery before production. Stop the old replica before
image/configuration updates to avoid overlapping SQLite writers. Back up and
restore the repositories, database, and Gitea configuration together; LRS
storage is not a backup. A move to PostgreSQL or another hosting substrate is
a separate scalability decision, not an implicit scale-out setting.

`WorkspaceRepositoryProvider.publish()` pushes only existing commits. Artifact
tools must use the accepted publication coordinator before calling it;
uncommitted cache content is not durable remotely. The harness never receives
Gitea credentials and never clones a workspace repository.

### Cross-Workspace Access

No chat, file, memory, plan, strategy, result, credential, or embedding crosses
into another workspace without an explicit audited export/import. The workspace
id is frozen at the residency and authorization boundary, and event streams and
artifact imports reject a source outside the selected workspace. Azure entity
reads validate the owner/workspace key binding, so a substituted `PartitionKey`
or `RowKey` fails closed.

### Prompt Injection

Uploaded and retrieved text is untrusted data, never an instruction source.
`thesis.operations.security.scan_untrusted_text` flags instruction-like
phrasing, and `thesis.agent.context.assemble_context` keeps untrusted data at
the lowest prompt precedence (data messages must not outrank trusted layers).
Flagged content is still delivered verbatim as evidence; it is never rewritten
or promoted to platform instruction.

### Secret Exfiltration

Git and payloads store references to a secret store, never secret values.
`assert_no_secret_material` rejects resolved tokens, bearer values, private
keys, and access-key identifiers in a payload; environment (`${NAME}`) and Key
Vault references are the accepted forms. Workspace manifests reject concrete
secret values at load time, and Foundry provider records never carry a token in
their representation.

### Confused Deputy

A model tool request is permitted only when the resolved role policy allows the
tool. The unified session registry rejects an unpermitted harness tool before any
transport exchange, and `assert_tool_permitted` fails closed for a tool outside
the role's allowlist.

### Stale Worker

A lease from an older intent epoch cannot mutate canonical state. A result
arriving after steering or after lease loss is retained as a late observation
(`RETAIN_LATE_RESULT`); a protected mutation from a stale lease is fenced and
rejected. Late results stay attached as evidence and are never silently
discarded.

### Repository Integrity

All durable workspace content lives in the workspace Git repository. Repository
growth is measured and enforced against both `max_repository_bytes` and
`max_artifact_bytes`. Reaching a budget pauses or rejects new artifact-producing
work; Thesis never silently prunes or externalizes existing evidence.

### Provider Authentication

Local startup performs an explicit provider-auth and model-deployment preflight
before autonomous work. Authentication failure leaves workspace and offline UI
access available and marks model-backed capabilities unavailable; it never
blocks local operation.

## Recovery Procedures

Recovery planning is read-only and deterministic. `RecoveryPlanner` inspects the
real queue, checkpoint store, and Git publication state and returns typed
decisions; the coordinator applies the authorized action through the owning
fenced API. Each scenario below names its symptom, evidence, action, and the
fail-closed condition.

### CHECKPOINT_INCOMPATIBLE (`checkpoint_incompatible`)

- Symptom: a workflow's durable checkpoint does not match the active runner's
  supported version, or the checkpoint projection cannot be read.
- Evidence: checkpoint id, `checkpoint_version`, and the compatibility error.
- Action: `RESUME_FROM_CHECKPOINT` (`resume_from_checkpoint`) when the
  checkpoint is absent or compatible; `MIGRATE_CHECKPOINT`
  (`migrate_checkpoint`) when an operator migration is required;
  `ENTER_RECOVERY` (`enter_recovery`) on a storage error.
- Fail-closed: an incompatible checkpoint is never replayed; it stays in
  `recovery` until migrated or explicitly failed. Use `fail_closed` plus
  `REQUIRE_OPERATOR` (`require_operator`) when no migration can be applied.

### QUEUE_INTERRUPTED (`queue_interrupted`)

- Symptom: a process restart left a workflow `running` or a job `leased`
  without a live worker.
- Evidence: workflow status, intent epoch, and the leased job ids.
- Action: `REQUEUE_INTERRUPTED` (`requeue_interrupted`) via
  `ExecutionQueue.recover`, which requeues in-flight jobs under the same ids
  and supersedes leases from an older epoch; `RESUME_FROM_CHECKPOINT`
  (`resume_from_checkpoint`) when no lease was interrupted.
- Fail-closed: a job whose intent epoch is stale is superseded, not replayed;
  a fenced publication attempt raises instead of mutating state.

### GIT_PUBLICATION_INTERRUPTED (`git_publication_interrupted`)

- Symptom: an accepted journal publication did not record a receipt, or a
  reachable Git commit exists without a receipt.
- Evidence: publication id, journal hash, and the matched (or missing) commit.
- Action: `RECORD_RECOVERED_PUBLICATION` (`record_recovered_publication`) when
  `find_publication` matches the exact manifest; `REPUBLISH_OUTBOX`
  (`republish_outbox`) when the outbox entry is still pending.
- Fail-closed: a commit that does not match the journal manifest triggers
  reconciliation failure; do not replay the logical mutation twice.

### PARTIAL_TOOL_COMPLETION (`partial_tool_completion`)

- Symptom: a workflow has incomplete tool work, or a stale worker returned a
  result after the workflow moved on.
- Evidence: job ids, job status, lease/fence tokens, and retained late results.
- Action: `RETAIN_LATE_RESULT` (`retain_late_result`) for stale results;
  `REQUEUE_PARTIAL_JOB` (`requeue_partial_job`) for unfinished jobs;
  `SUPERSEDE_STALE_RESULT` (`supersede_stale_result`) when stale-epoch jobs are
  superseded; `RESUME_FROM_CHECKPOINT` (`resume_from_checkpoint`) when no
  partial work remains.
- Fail-closed: a late result is attached as evidence and cannot mutate
  canonical strategy, plan, contract, or findings until reevaluated under the
  current intent epoch.

## Repository Size, Backup, Restore, And Retention

Measure with `measure_repository` through the injectable `RepositorySizePort`;
the default local adapter walks the tree deterministically and counts artifact
subtrees (`backtests/`, `uploads/`, `activations/`) separately. Enforce with
`enforce_budget`, which checks **both** the total repository ceiling and the
artifact ceiling and raises `RepositoryBudgetExceeded` naming the breach.

Back up with `create_backup`: a deterministic, content-addressed archive of the
repository tree, written locally with no network access. Restore with
`restore_backup`, which verifies the receipt digest and size before extracting
and rejects unsafe member paths. Neither operation deletes or externalizes an
existing artifact.

Retention is planned with `plan_retention`. The policy keeps the newest
`max_versions` commits plus every protected commit; protected commits are never
pruned, regardless of age. Retention changes Git history only through the
normal publication path; the operation never touches a deployed revision's
source.

## Provider Authentication Failure

When Foundry authentication is unavailable:

1. Workspace and offline UI access remain available; do not block local work.
2. Model-backed capabilities are marked unavailable. `classify_provider_auth`
   reports the provider reason while keeping `offline_workspace_available`
   true.
3. Restore access through an interactive developer sign-in or managed identity
   outside the repository. Never write a token, key, or connection string into a
   workspace, log, or ticket; reference the secret store instead.
4. Re-run the preflight and confirm the provider reason returns to `ready`
   before resuming autonomous work.

## Verification Commands

Run from the repository root:

```powershell
uv run --extra dev pytest -p no:cacheprovider tests/thesis/operations -q
uv run --extra dev ruff check src/thesis/operations tests/thesis/operations/test_security.py
uv run --extra dev ruff format --check src/thesis/operations tests/thesis/operations/test_security.py
uv run --extra dev ty check src/thesis/operations
```

## Safety Limits

- Recovery planning is read-only; only the coordinator applies an authorized
  action through the fenced queue/checkpoint/publication API.
- A budget breach pauses or rejects new artifact-producing work; it never
  deletes or externalizes existing evidence.
- Retention preserves protected commits unconditionally and uses the configured
  version window for everything else.
- Backup and restore operate on local paths only and verify content identity
  before restore.
- Provider credentials are process-scoped and are never committed, logged, or
  sent to browser code.

## Drill Record

| Date | Scenario | Action taken | Outcome |
| --- | --- | --- | --- |
| (pending) | CHECKPOINT_INCOMPATIBLE (`checkpoint_incompatible`) | MIGRATE_CHECKPOINT (`migrate_checkpoint`) | Not yet drilled |
| (pending) | QUEUE_INTERRUPTED (`queue_interrupted`) | REQUEUE_INTERRUPTED (`requeue_interrupted`) | Not yet drilled |
| (pending) | GIT_PUBLICATION_INTERRUPTED (`git_publication_interrupted`) | REPUBLISH_OUTBOX (`republish_outbox`) | Not yet drilled |
| (pending) | PARTIAL_TOOL_COMPLETION (`partial_tool_completion`) | RETAIN_LATE_RESULT (`retain_late_result`) | Not yet drilled |
