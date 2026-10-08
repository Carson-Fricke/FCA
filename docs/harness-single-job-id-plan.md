# Harness Single Job ID Implementation Plan

## Status

Implemented and integration-verified in this worktree. This document began as
the implementation plan and now records the contract that landed.

Verification (one integration pass plus one permitted rerun after a
test-fixture fix): `uv lock --check`, repository Ruff lint/format, `ty`, and
the full pytest suite (1365 passed, 1 skipped); harness Rust workspace
`cargo fmt --check`, `clippy -D warnings`, and `cargo test --workspace` (all
green; run with a redirected `CARGO_TARGET_DIR` while a pre-migration local
server held the default target binary); lake Rust workspace fmt/clippy/tests;
UI wire-generation check, Vitest (255 passed), and production build. The UI
`svelte-check` baseline remains 37 pre-existing errors unrelated to this work.

Follow-up hardening: the harness no longer generates an execution ID anywhere.
`job_id` is required in configuration; `run_backtest`/`run_study`/`run_screener`
accept an optional submission `job_id` that replaces a document value; Thesis
allocates `job-<opaque>` before activation and screening, fails closed on a
mismatched acknowledgement, and the local stack verifier passes 5/5 with the
allocated ID. The follow-up cleanup (artifact-free failure records, durable
submission recovery, neutral `jobs/` artifact prefix, job-based digests, and
screening schema v2) is specified in
`docs/harness-job-contract-cleanup-plan.md`.

The operator authorized deletion of old harness runs and artifacts and excluded
preservation of in-flight executions. Local `.local/harness-results` (catalog,
jobs, objects) and `.local/backtests` were deleted; market-lake data and live
trader state were preserved. No aliases, dual-read decoders, translation
tables, deprecated routes, or compatibility environment variables remain.

Deployed environments still need the scoped operational cutover (harness
results container and lake catalog Azure table rows written under the retired
names) and a restart of any stack still running the pre-migration binaries.

## Objective

Allocate one `job_id` for one harness execution and use it unchanged through
Thesis submission, harness admission, execution, status, artifacts, evidence,
workspace reports, and UI links.

Remove independently generated execution identifiers and their backend fields:
`run_id`, execution `study_id`, `backtest_id`, `submittedRunId`, and
`tool_job_id`. Remove operation-level `correlation_id` where it duplicates the
job identity. This is not a UI-only relabeling.

## Target Contract

1. `job_id` is allocated once before any execution side effect. Use a bounded,
   path-safe opaque value, such as `job-<uuid4 hex>`, without embedding program
   names, workflow identities, or configuration hashes.
2. Submission composition owns allocation. Thesis allocates before its local
   tracking record and network request; direct harness CLI/API/MCP callers
   allocate at their submission boundary. Workers, engines, importers, and
   renderers never allocate another identity for that execution.
3. One submitted execution has one immutable accepted input. A duplicate
   submission with the same ID and accepted input returns existing state without
   dispatching again. The same ID with different input is a conflict. Admission
   must enforce this atomically, including concurrent submissions.
4. An explicit rerun gets a fresh ID. Recovery or retry of submission inspects
   the original ID; it never silently starts a new execution or overwrites a
   terminal result.
5. The ID exists for not-started failures, queued jobs, running jobs, terminal
   jobs, and unknown submission outcomes. A completed artifact set is not
   required for a job record to exist.
6. There is one authoritative harness job record, not separate job-lifecycle
   and result-catalog records with independent state. Artifacts and optional
   result metadata attach to that record using `job_id`.
7. Thesis may retain a workspace-scoped tracking/report projection, but its key
   is `(workspace_id, job_id)`. It does not allocate a report or tool-job ID.
   Harness state remains authoritative for execution; Thesis owns attribution
   and submission diagnostics.
8. Python/Rust records, configurations, JSON receipts, MCP arguments, manifests,
   and UI execution references use `job_id`. Replace obsolete camel-case
   execution keys too rather than accepting alternate spellings.
9. Configuration/content digests are provenance and cache keys, not job IDs.
   Screening uses fresh execution IDs rather than `screen-<config digest>`.
10. A deliberate live-process resume uses the existing job ID and validates
    its state/configuration. A new deployment execution gets a fresh job ID;
    deployment name is not execution identity.

Example submission receipt:

```json
{
  "job_id": "job-0123456789abcdef0123456789abcdef",
  "kind": "study",
  "state": "queued"
}
```

Successful Thesis program receipts expose `job_id` at the top level alongside
program/version attribution. A nested result can contain metrics and artifact
details, but no alternate execution-ID fields. An unknown-outcome receipt keeps
the preallocated top-level `job_id`; a mismatched harness acknowledgement is a
protocol failure, not permission to adopt another ID.

## Locked Implementation Contract (Wave 1)

This section is normative for implementation. Where any other document or
source comment disagrees, this section wins. All active execution identity is
`job_id`. Wire spellings: harness JSON/config/MCP = `job_id`; Thesis domain
summaries = `jobId`; Thesis durable record keys = `job_id`; UI/API =
`job_id`. State vocabulary: `queued`, `running`, `succeeded`, `failed`,
`cancelled`. There is no `complete` state, no `succeeded` alias variant, no
study execution alias, and no report identity.

### Harness (Rust)

- `store.rs`: one `JobRecord` and `StoredJob`. Delete `RunStatus`,
  `RunCatalogRecord`, `StoredRun`, `JobRepository`, and `RunStore`.
  - `JobKind { Backtest, Study, Screen }`.
  - `JobState { Queued, Running, Succeeded, Failed, Cancelled }`.
  - `JobRecord` carries kind, state, timestamps, error, `strategy_id`, data
    bounds, `run_digest`, `configuration_digest`, artifact count, total bytes,
    metrics, and `input_digest` for admission.
  - `JobStore`: `get`, `admit`, `put_result`, `list`, `fetch`, `delete`.
    `admit` is atomic create-or-join on `(job_id, input_digest)`; persistence
    failure must surface as an error, never a swallowed write.
  - Filesystem layout: `<root>/jobs/<job_id>.json`; artifact objects live under
    `<root>/objects/jobs/<job_id>/...` (the cleanup plan renamed the prefix).
- HTTP: kind-specific POST submission stays. Reads become
  `GET /v1/jobs/{job_id}`, `GET /v1/jobs`,
  `GET /v1/jobs/{job_id}/artifacts`,
  `GET /v1/jobs/{job_id}/artifacts/{*name}`, and `DELETE /v1/jobs/{job_id}`.
  `Location` is `/v1/jobs/{job_id}` for every kind. Delete
  `/v1/backtests/jobs/{job_id}`, `/v1/backtests/runs*`, and
  `/v1/screeners/jobs/{job_id}`. `LaunchAcceptedResponse { job_id, state }`.
  A launch whose outcome did not complete is `Failed`, never `Succeeded`.
- MCP: identity arguments are `job_id`; `get_run` becomes `get_job_result`;
  `get_job_status`, `list_artifacts`, and `read_artifact` keep their names with
  `job_id`. Submission receipts `{boundary, job_id, state, kind}`; result
  receipts carry `job_id`, `state`, and metrics/artifacts. Hosted-local and
  REST-forwarding surfaces must stay equivalent.
- Native submission and execution: parse the configuration once at submission
  and pass the parsed configuration to background execution; never reload the
  mutable config path. `job_id` is required in configuration and is never
  generated by the harness; submission tools accept an optional `job_id`
  argument that overlays a document lacking one (a conflict when it differs),
  and it is always present in receipts and records.
- Configuration: `run_id` becomes `job_id` in backtest/study/screen
  configuration. Screening uses a fresh execution `job_id` and keeps
  `configuration_digest` as provenance. Digest domain strings and
  `run_digest` stay unchanged; they are provenance, not identity.
- Live: `TraderConfig.job_id`, environment `HARNESS_TRADER_JOB_ID`; resolve one
  identity for supervisor, config, journal, snapshot, and fee deduplication.
  Deployment name is not execution identity.

### Thesis (Python)

- Fresh IDs use `job-<opaque>` through the existing ID factory. Every harness
  operation is allocated once at submission and passed unchanged to tracking,
  runner configuration, evidence, and reports.
- Harness activation carries the allocated `job_id` in its payload; adapters
  require it for backtests and fail closed on a mismatched acknowledgement.
  Screening allocates once per run and reuses that ID for launch, status,
  artifacts, and import; the live trader path resolves its own persisted ID.
- `ToolJob.job_id` (was `tool_job_id`); links `job_ids`; event payloads
  `jobId`; persistence keys `job_id`/`job_ids`; HTTP chat records use `jobId`.
  Delete `ToolJob.correlation_id`; event correlation uses `job_id`.
  `submit_tool_job` accepts an optional `job_id` and no longer allocates a
  separate correlation.
- Remove alias fallbacks; receipts expose one top-level `job_id`; delete
  `submittedRunId`. `BacktestRunRequest.job_id`;
  `ResearchModelRunRequest.job_id`.
- `BacktestRecord`: delete `id`; use `job_id`. Delete `backtest_id` from cards,
  reports, and routes; workspace reports key on `(workspace_id, job_id)` with
  atomic add-if-absent materialization.
- Terminal jobs are joined and reused, never relaunched under the same ID. An
  explicit rerun allocates a new `job-` ID. Falsification and resume paths
  allocate `job-<opaque>` on first execution, persist it in the trial/phase
  record, and reuse the persisted value on rebuild; delete hash-derived
  recomputation.
- Discovery: delete the required `study_id`; executed attempts carry optional
  `jobId`.
- Pending and success comparisons use only the locked state vocabulary.

### Lake And UI

- Lake catalog: keep class names; rename the harness-result mirror field
  `run_id`/`RunId` to `job_id`/`JobId`; align states to the locked vocabulary;
  update Python/Rust models, repositories, Azure entity mapping, parity/oracle
  fixtures, and tests. Leave lake ingestion/ensure job types untouched.
- UI: one visible Job ID (`job_id`); delete `run_id`, `study_id`,
  `backtest_id`, and `tool_job_id` execution fields; regenerate
  `src/ui/backend-wire.json` and generated artifacts after the Thesis HTTP
  routes and schemas land.

## Scope Boundaries

- Include backtests, observational studies, screening, paper execution, and
  live/trader execution identity and their status/artifact consumers.
- Include the lake catalog's harness-result contract in Python, Rust, and Azure
  serialization. Its current `RunRecord` is a parallel result contract, not a
  verified live connection to the harness filesystem store.
- Rename the chat `ToolJob` identity field to `job_id`, including generic tool
  submissions. A generic non-harness tool job still has its own job ID; it does
  not create a harness execution. A harness-producing tool job passes its one ID
  into the harness rather than allocating a child identity for the same work.
- Keep provider tool request/call IDs and message IDs required for explicit
  request/result correlation. Status queries and artifact reads are distinct
  tool calls referencing an existing job; they do not create execution jobs.
- Keep workspace, conversation, workflow, program, strategy, order, fill, and
  actual parent/child job identities. One workflow can launch multiple jobs and
  multiple falsifier evaluations can reference one job.
- Remove discovery's ambiguous required `study_id` field. Executed attempts
  reference `job_id`; unexecuted attempts use descriptive fields without an
  invented execution ID. If authored-program attribution is needed, reference
  the existing actual `program_id`, not a replacement study identity.
- Do not merge lake ingestion/ensure jobs into harness executions. Do not
  implement an optimizer, resurrect the removed public sweep API, or introduce
  a distributed scheduler or generalized attempt framework.
- Preserve the neutral session inbox, canonical incoming writer, explicit
  provider tool correlations, and shared live/replay provider projection.
  Job tracking must not introduce another tool-result delivery path.

## Current Removal Inventory

| Area | Current Source | Required Change |
| --- | --- | --- |
| Harness persistence | `src/harness/mcp/src/store.rs` | Replace `JobRecord` plus `RunCatalogRecord` and their separate repositories with one authoritative job record and storage boundary. Preserve independent artifact payload storage. |
| Harness launch/status | `src/harness/api/src/http/models.rs`, `launcher.rs`, `routes/launch.rs`, `routes/runs.rs`, `routes/screeners.rs`, `screener.rs` | One receipt, lifecycle, result reference, and ID across all kinds. Remove job-to-run resolution. |
| Local/native execution | `src/harness/api/src/local.rs`, `native.rs`, `mcp_cli.rs`, backtest configuration/job/manifest/recorder modules | Remove receipt aliases; propagate `job_id` through engines, configuration, output paths, and manifests. |
| MCP contracts | `src/harness/mcp/src/tools.rs`, `lib.rs` | All launch/status/result/artifact arguments and receipts use `job_id`; remove local/remote divergence. |
| Screening identity | `src/harness/backtest/src/backtest/screen.rs` | Separate execution allocation from configuration digest; pass `job_id` to every screening path. |
| Trader identity | `src/harness/live/src/supervision.rs`, `fills/fees.rs`, `src/harness/backtest/src/runtime/paper.rs`, `state_store.rs` | Resolve one ID for supervisor/config/journal/snapshot/fee deduplication. Remove deployment-name identity and split environment override behavior. |
| Shared lake result catalog | `src/lake/catalog/models.py`, `ports.py`, `memory.py`, `__init__.py`, `src/lake/catalog/src/{models,ports,memory,azure,lib}.rs` | Replace harness-result `run_id`/`RunId` keys with `job_id`/the chosen job entity key; update parity fixtures and row-key construction. Keep lake job types semantically separate. |
| Thesis chat tracking | `src/thesis/chat/{contracts,service,persistence,azure}.py`, `src/thesis/http/chat.py`, Cosmos record adapters | Replace `tool_job_id` and plural links with `job_id`/`job_ids`; remove independent harness-operation correlation allocation. |
| Thesis submission | `src/thesis/workspaces/programs.py`, `agent/runtime/program_tools.py`, `integrations/{mcp,harness,harness_activation,runtime_status}.py`, `http/harness.py` | Allocate once, propagate unchanged, validate acknowledgements, return one top-level ID, and remove fallback extraction. |
| Thesis reports | `src/thesis/backtests/{coordinator,run_import,artifacts}.py`, `src/thesis/http/backtests.py` | Delete `bt-*` generation, record `id`, `backtest_id` responses, and job-to-run-to-report lookup. Use direct workspace/job lookup with atomic uniqueness. |
| Workflow evidence | `src/thesis/workflows/{backtests,local,research_models,evidence}.py`, hypothesis-development jobs/trials/discovery/contracts/prompts, research models/lineage | Replace `runId`, `jobId`, `backtestJobId`, and `studyId` execution references with `job_id`; retain genuine definition/parent references. |
| UI | `src/ui/src/lib/{api,types}.ts`, chat tool-result cards, backtest components/routes, fixtures | One visible Job ID and one report lookup key. Remove ID fallback chains and extra identifiers from generic study JSON rendering. |

Before editing, expand this inventory with focused searches of source, tests,
configuration, scripts, active runbooks, and architecture. Classify each match
by semantics; do not blanket-replace unrelated correlations or historical
verification records.

## Implementation Tasks

### 1. Lock the Contract and Ownership

- Define one job lifecycle vocabulary shared by admission, status, and terminal
  publication. Retain real cancellation/not-started distinctions without
  inventing unsupported scheduler behavior.
- Define typed launch/status/result receipts rather than relying on opaque
  dictionaries and fallback key precedence.
- Specify the accepted-input fingerprint used for admission conflict detection.
  Keep submitted-input and resolved-configuration provenance distinct where
  they have different meanings; an execution ID is not a content hash.
- Identify every actual persistent store root and external result partition
  before destructive cleanup. Shared containers must not be wiped wholesale.
- Assign an exclusive owner to every edited file. The coordinator owns shared
  entrypoints, schema generation, architecture documents, delivery guards, and
  integration verification unless ownership is explicitly reassigned.

Completion: agreed schemas, removal list, cleanup boundaries, and non-overlapping
file ownership. No compatibility surface is included.

### 2. Replace Harness Persistence and Admission

- Introduce the canonical job record in the existing owning capability. It
  contains `job_id`, kind, lifecycle state, timestamps, diagnostics,
  accepted-input identity, and optional result/artifact metadata.
- Remove the two independent job/catalog record models and duplicate lifecycle
  enums. Replace the filesystem and in-memory implementations and composition
  sites together. Use one metadata document keyed by `job_id`.
- Support metadata-only records before execution and after failures without
  artifacts. Result publication attaches durable artifact references and only
  then publishes success.
- Make admission create-or-join atomic. Persistence failure prevents acceptance
  or dispatch; do not swallow failures as the existing job upsert does.
- Capture accepted configuration/source references before dispatch. Workers must
  not reload a mutable config path and publish output from different input under
  the accepted ID.
- Reject destructive terminal-result replacement. Delete-by-job removes its
  metadata and owned artifacts, including lifecycle data.

Completion: one authoritative harness record works for every lifecycle stage;
concurrent duplicates dispatch once and conflicting input is rejected.

### 3. Propagate Identity Through Every Harness Boundary

- Replace configuration `run_id` and execution context fields with `job_id`
  across deterministic execution, manifests, recorder outputs, paper execution,
  fee aggregation, and live state.
- Make direct CLI/API/MCP submission allocate or accept the one preallocated
  ID; remove fixed sample execution IDs and worker-side allocation. No extra
  submitted-ID field or separate idempotency ID is needed for harness admission.
- Give screening the same execution identity policy as backtests and studies.
  Preserve its full configuration digest for caching and conflict detection.
- Use neutral result/status routes for all kinds, for example
  `/v1/jobs/{job_id}`, `/v1/jobs/{job_id}/results`, and
  `/v1/jobs/{job_id}/artifacts`. Submission can remain kind-specific. Remove
  obsolete query routes rather than leaving redirects or aliases. Every returned
  `Location` must resolve.
- Keep MCP operation names that still express a real operation, but update all
  schemas and implementations to `job_id`. Verify hosted-local and REST-forwarding
  execution use identical contracts.
- Update live configuration and environment names, status/resume commands,
  snapshots, and journals together. Resolve the ID once; preserve fill
  deduplication and resume safety semantics.
- Update the lake harness-result record/entity contract and parity fixtures.
  Preserve unrelated lake ingestion behavior and avoid type-name collisions
  with its existing ingestion `JobRecord`.

Completion: no active harness execution contract uses `run_id` or execution
`study_id`; no result consumer needs a job-to-run conversion.

### 4. Unify Thesis Tracking, Submission, and Reports

- Change chat `ToolJob` records, persistence keys, links, events, HTTP schemas,
  and callers to `job_id`. Preserve lifecycle, attribution, intent-epoch
  fencing, and durable late-result handling.
- For harness operations, allocate before tracking and pass that same ID into
  the runner/configuration. Remove the current separate tool-job ID and
  correlation-as-run-ID flow. An explicit rerun allocates anew; repeated
  submission of an existing job does not create another tracking record.
- Retain event/provider correlations for genuinely distinct events and tool
  calls. Do not rename or collapse `ToolCorrelation.request_id` or
  `request_message_id` into the job ID.
- Standardize committed-program receipts at the top level so coordinator and
  UI do not miss IDs nested under `result`. Reject a mismatched returned ID;
  preserve the original ID in unknown-outcome diagnostics.
- Replace request/result workflow fields and evidence producers together.
  Remove alias chains and terminal same-ID relaunch behavior. Resume inspects
  or joins the existing job; an authorized new execution gets a fresh ID.
- Remove discovery's required `study_id`, update decoding/prompts/rebuild, and
  associate executed attempts with their actual job. Multiple falsifiers of
  one execution keep the same job reference and their separate evaluation IDs.
- Remove report `id`, `bt-*` factories, `backtest_id`, and mapping helpers. Use
  direct `(workspace_id, job_id)` lookup and atomic add-if-absent materialization.
  Both workspace launch and later import converge on this projection.
- Update activation, HTTP/MCP clients, runtime status, artifact access, evidence
  hashes, and shared serializers. New hashes reflect new bytes; do not rewrite
  historical verification records.

Completion: tracking, execution, and report all refer to the same allocated
value, including prelaunch failures and unknown outcomes.

### 5. Update UI, Generated Contracts, and Active Documentation

- Use Job ID consistently in launch notifications, progress/result cards,
  report URLs, history, and artifact views. Remove `run_id`, `study_id`,
  `backtest_id`, and `tool_job_id` execution fields from manual UI interfaces.
- Update workspace route parameters and fixture keys together. Generic study
  receipts must also expose only the canonical execution identity.
- Regenerate the backend OpenAPI snapshot and frontend types/schema; never
  manually edit generated outputs. Add explicit tests for opaque tool receipts
  because OpenAPI generation alone does not cover them.
- Update sample configurations, smoke scripts, environment examples, trader
  runbooks, and affected source-folder READMEs.
- Update affected architecture documents in the same implementation change:
  `.github/architecture/repository.md`, `fca-thesis.md`,
  `rust-interface-migration.md`, `screener-rust-migration.md`,
  `thesis-agent-workflow-runtime.md`, and `thesis-ui-design.md` where their
  contracts change. Describe implemented behavior only after it lands.

Schema generation from repository root:

```powershell
uv run --extra dev python -m thesis.http.schema --output src/ui/backend-wire.json
```

Frontend generation from `src/ui`:

```powershell
npm run generate:wire
npm run generate:wire -- --check
```

Completion: chat and history open the same workspace job report, and all active
documentation and generated contracts describe one ID.

### 6. Clean Cutover

- Stop the affected harness/Thesis processes; discard rather than drain or
  reconcile old in-flight simulation jobs. Do not build in-flight migration code.
- Delete old harness job metadata, result catalogs, run artifacts, temporary
  execution output, and obsolete workspace backtest projections from verified
  configured locations. Remove external harness-result rows/objects only within
  their identified scope.
- Do not erase market-lake bars/coverage, authored workspace programs, unrelated
  chats, workflow records, source fixtures, or historical verification records.
  Old report links are unsupported; no compatibility resolver is required.
- Live trading positions, broker orders, and safety/recovery journals are not
  covered by permission to delete old simulation runs/artifacts. If old live
  state exists and must be removed, obtain specific authorization before deleting
  it or restarting with empty safety state. Do not add legacy decoders instead.
- Start the new backend/frontend together on empty affected job/result stores.
  Run fresh study, backtest, and screening smokes with fake/injected data. Live
  identity tests use fake broker/state ports, never real trading.
- Do not delete old data automatically on application startup or read failure.
  Cleanup is an explicit scoped operational step, not permanent runtime code.

Completion: fresh executions work without old records, converters, or fallback
paths. No old run/artifact preservation work is performed.

## Execution Order

1. Coordinator locks schemas, removal boundaries, shared-file ownership, and
   cleanup scope.
2. Harness owner implements persistence/admission and execution propagation as
   one coherent change; dependent consumers wait for the contract to settle.
3. Once interfaces are stable, separate owners may update Thesis tracking and
   report code, lake result contracts, and UI in parallel only with exclusive
   file ownership. Workflow/shared chat models remain explicitly owned.
4. Coordinator integrates receipts, generated schemas, architecture documents,
   cross-product tests, and scoped cleanup instructions.
5. Freeze all edits, run one integration verification pass, perform the scoped
   clean cutover, and record fresh-job smoke evidence.

Subagents run focused checks only. The coordinator owns repository-wide
verification. One additional full pass is allowed only after fixing a failure
from the first pass. Isolate a hang with a hard timeout; do not rerun an unchanged
full suite.

## Acceptance Tests

| Scenario | Required Assertion |
| --- | --- |
| Full lifecycle | Submission, tracking, state, engine manifest, artifacts, evidence, report, and UI URL use exactly the same `job_id`. |
| Backend removal | Old execution fields, ID generators, separate harness lifecycle/catalog repositories, and translation helpers are absent from active owned paths. |
| Concurrent duplicate | Same job/input submitted concurrently dispatches once and returns existing state. |
| Input conflict | Same ID with different accepted input fails explicitly without changing the original job. |
| Intentional rerun | Same program/config executed again receives another job ID and leaves the first result untouched. |
| Immutable admission | Editing a source config path after acceptance cannot change the accepted job input. |
| Failure before results | Failed/not-started jobs remain inspectable without artifacts or a second identity. |
| Unknown outcome | Original job ID remains available; transport failure does not trigger fresh execution or alias guessing. |
| Acknowledgement mismatch | A different ID returned by the harness fails closed without replacing the submitted identity. |
| Publication failure | Metadata/artifact persistence failure cannot appear as successful acceptance/completion. |
| Surface parity | CLI, REST, hosted MCP, and remote MCP propagate one contract; status locations resolve for all kinds. |
| Workspace import | Repeated/concurrent materialization creates one projection per workspace/job, with workspace authorization enforced. |
| Program receipt | Actual committed-program receipt reaches coordinator/card/report without nested-ID extraction failures. |
| Discovery/falsification | No invented study identity; multiple evaluations can reference one job without extra executions. |
| Screening | Two explicit executions get distinct job IDs while preserving identical configuration digests where appropriate. |
| Trader/paper | Supervisor/config/state agree on job identity; deliberate resume retains identity and fill deduplication. |
| Session delivery | Job changes preserve one canonical incoming writer and explicit provider request/result correlations. |
| Removed interfaces | Old configuration fields, MCP argument keys, ID aliases, and replaced routes are rejected rather than translated. |

Extend existing tests under the owning Rust crates' `tests/` directories and
mirrored Python capabilities. Relevant suites include harness API HTTP/MCP
parity and configuration contracts, backtest configuration/manifest tests,
lake catalog oracle tests, Thesis workflows/local/programs/harness HTTP tests,
and UI wire-contract/generation tests. Add focused coordinator/importer tests
and a static removal guard scoped to obsolete execution contracts; do not ban
unrelated tool correlations or historical evidence globally.

Replace tests that assert superseded IDs or same-ID terminal relaunch. Do not
keep production adapters solely to satisfy their fixtures. Never put Rust test
bodies in `src/**/*.rs`; follow `.github/instructions/rust.instructions.md`.

## Verification

Python checks run through `uv` from repository root. Owners use focused paths;
the coordinator runs these commands once after the implementation freezes:

```powershell
uv lock --check
uv run --extra dev ruff check .
uv run --extra dev ruff format --check .
uv run --extra dev ty check
uv run --extra dev pytest -p no:cacheprovider
```

The test pass must include
`tests/thesis/agent/runtime/test_delivery_architecture.py` and affected arrival,
tool-result, and live/replay behavior tests. Do not treat documentation or AST
guards alone as proof of lifecycle correctness.

From `src/harness`, follow the Rust workspace verification commands:

```powershell
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
```

From `src/lake`, run the same Rust commands for the affected shared catalog
contract. Python lint/format/type checks remain Ruff/ty; do not substitute other
Python tooling.

From `src/ui`:

```powershell
npm run generate:wire -- --check
npm run check
npm run test
npm run build
```

Run targeted UI end-to-end checks for launch-to-report and chat-to-report where
the existing test harness supports them. Record any unavailable verification
explicitly. Apply relevant infrastructure/workflow checks if those files change.

## Definition of Done

- Exactly one execution ID is generated and persisted across the job lifecycle.
- No independent tool-job, study-execution, run, or backtest-report identity
  remains in the backend, receipts, configuration, or UI.
- One authoritative harness record owns lifecycle and result metadata; workspace
  projections reference it with the same ID.
- Old artifacts and affected job/result stores are removed by scoped cleanup,
  without data conversion or compatibility behavior.
- Real program/workflow/provider-call identities and trading safety semantics
  remain intact.
- Current tests, generated contracts, active documentation, and fresh-job smokes
  verify the new contract, with no fabricated verification claims.
