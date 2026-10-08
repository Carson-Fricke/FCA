# Harness Job Contract Cleanup Plan

## Status And Scope

Implemented and integration-verified in this worktree: failure outcomes publish
without fabricated artifacts, submission identity is durably retained with
attribution and reconciled on interruption, and public vocabulary/artifacts use
the neutral `jobs/` contract. The plan addressed debt items 3-6: fabricated
failure artifacts, inconsistent public terminology, duplicated
submission/recovery policy, and unclear tracking ownership.

Verification (coordinator integration pass plus one rerun after fixing test type
errors): `uv lock --check`, repository Ruff lint/format, `ty`, full pytest
(1393 passed, 1 skipped); harness Rust workspace `cargo fmt --check`,
`clippy -D warnings`, `cargo test --workspace`; UI wire-generation check,
Vitest (255 passed), production build, `svelte-check` at its 37-error
pre-existing baseline. Live smoke after restarting the local stack: verifier
5/5; a new backtest that could not run finished `failed` with a truthful error
and **zero artifacts**; a failed job's materialization card returned
`{"job_id", "state": "failed"}` with no redundant `status` alias.

Open findings (pre-existing, not introduced by this cleanup): the workspace
launch path stores a non-terminal projection before the harness import, so a
launched job's report card can remain `state: queued` after terminal failure
until a terminal import refresh is added; the sample local backtest fails on a
lake `/v1/read` decode error (lake/harness local contract), which the verifier's
launch-acknowledgement stage does not cover. Neither affects the contracts or
tests above.

Retain one execution `job_id`. Do not restore `tool_job_id`, execution
`study_id`, `run_id`, or an operation-level correlation ID. Provider tool-call,
message, workflow, program, and genuine parent/child job identities remain
distinct. A content digest remains provenance, not another execution ID.

No compatibility aliases, old-format decoders, generalized submission engine,
distributed scheduler, or optimizer work. Do not fix unrelated UI type errors.
Old runs/artifacts are disposable under the existing operator authorization;
cleanup must stay scoped, explicit, and outside application startup.

## Slice 1: Job Outcomes And Tracking Ownership

### Harness Outcomes

- Adjust the existing `JobStore::put_result` contract so failed/cancelled jobs
  may publish terminal metadata with an empty artifact set. Keep the existing
  successful-result artifact requirement unless a real execution kind requires
  artifact-free success; do not broaden it speculatively.
- Keep truthful errors in `JobRecord.error_message`. Preserve real partial
  artifacts produced before failure, including actual engine diagnostics.
- Remove `error_artifact()` and synthetic failure-only `error.json` writers.
  Do not replace them with another invented file or special artifact namespace.
- An existing artifact-free failed job returns an empty artifact list; an absent
  job returns not found. Failure remains inspectable through its job record.
- Commit terminal metadata only after any supplied artifacts are durably written.
  A publication failure must not appear as a successful terminal write, erase
  partial evidence, or permit terminal-result replacement.

### Thesis Tracking

- Make the responsibility boundary explicit: harness records own execution
  state/results; Thesis tracking owns attribution, submission certainty, and
  late-result observation. Tracking/report records use the same `job_id` and
  must not become another execution-state authority.
- Classify harness-backed versus non-harness tracking at a trusted submission
  boundary. Use existing operation/tool metadata where it is reliable; add one
  explicit classification field only if consumers cannot distinguish them
  without guessing. Do not allocate another ID to solve classification.
- Ensure harness report/status consumers exclude non-harness tool jobs. A tool
  job that failed before dispatch must remain truthful about not-started versus
  unknown outcome rather than claim a harness execution failed.
- Keep job events grouped by `job_id`; this is useful correlation, not redundant
  identity. Preserve independent provider request/result correlation and the
  canonical incoming delivery path.

Primary ownership: `src/harness/mcp/src/store.rs`, harness HTTP/local
publication paths, `src/thesis/chat/{contracts,service,persistence}.py`, and
affected report/status consumers. Update their owning tests in the same slice.

Acceptance: metadata-only failure, partial-output failure, publication fault,
empty-versus-absent artifact listing, and generic non-harness tracking tests
pass; no synthetic artifact writer or extra execution ID remains.

## Slice 2: Submission And Recovery Rules, Including Instructions

Do not build a helper that allocates, submits, polls, imports, persists, and
schedules everything. Consolidate only duplicated pure acknowledgement/state
decoding in the existing integration capability when it has real consumers.
Keep side effects and ownership visible at the call sites.

### Implementation Rules

1. Submission composition allocates `job_id` once and durably retains the
   recoverable submission reference before the external mutation. Store enough
   immutable input/program-version attribution to reconcile the same operation.
2. Harness admission alone decides create/join/conflict: same ID and accepted
   input joins; different input conflicts; terminal results cannot be overwritten.
   A client lookup may optimize a join, but must not act as admission authority.
3. Client adapters validate acknowledgement identity. Missing/mismatched IDs are
   protocol failures; never adopt another ID or infer one from aliases/prose.
4. Transport interruption after possible dispatch is unknown outcome, not proof
   of absence. Recovery inspects/reconciles the retained ID. It never allocates
   a replacement or automatically replays an uncertain mutation.
5. An intentional new execution allocates a fresh ID. Resume reuses the original
   durable reference; repeated tracking registration must not reset timestamps,
   duplicate links, or overwrite prior evidence.
6. Waiting remains caller-specific: HTTP returns acceptance, deterministic code
   may wait, and agents may inspect conversationally. The workflow engine does
   not acquire a turn loop, operator gate, polling policy, or harness knowledge.

### Close The Interruption Window

- In falsification, locate an already persisted submission through the owning
  workflow/phase attribution, not only `state.trials`. Completed trial records
  remain evidence but cannot be the sole recovery reference.
- Reuse the existing tracking/execution persistence boundary if it can retain
  this reference; do not introduce a second journal or another correlation ID.
  Repeated registration validates the existing operation/input instead of
  replacing it. Reject ambiguous or contradictory prior references.
- Apply the same retain-before-dispatch and truthful-outcome rules to program,
  HTTP activation, and screening submissions. Responses/errors must expose the
  retained `job_id` where the caller needs it for inspection.
- Keep live trader resolution separate: resume uses persisted identity; a new
  session gets a fresh identity. Do not fold trading recovery into simulation
  submission logic or delete safety state as part of this slice.

Primary ownership: `src/thesis/integrations/**`, program/HTTP/screening
submission callers, `workflows/local.py`, hypothesis-development jobs/trials
and rebuild code, and the tracking boundary from Slice 1. Preserve native
session/provider boundaries.

### Instruction Changes In The Same Slice

Update existing loaded instructions rather than create an unreferenced policy
file. Add a concise job-contract section to
`.github/instructions/copilot-instructions.md`; add the workflow-specific
application to `.github/instructions/thesis-workflow-development.instructions.md`.
Add a short reference/enforcement reminder to root `AGENTS.md`. Keep the
architecture document as the design source of truth.

The instruction text must encode these requirements:

- Submission composition allocates and durably retains identity before dispatch;
  engines, workers, importers, and renderers never invent a replacement.
- Harness admission owns uniqueness; adapters own acknowledgement validation;
  callers own waiting. Do not add a universal submission/turn/polling service.
- Unknown outcome requires inspection of the original ID, not automatic replay.
- Resume reuses the durable submission reference even before trial/report
  evidence exists; explicit rerun gets a new ID.
- Harness tracking and execution share `job_id`; non-harness tracking is
  explicitly distinguishable. Job event correlation may use the same ID.
- New submission paths must include interruption/mismatch coverage. Instructions
  do not prove compliance; executable behavior tests are mandatory.

Acceptance: interrupt after harness acceptance but before trial/report storage,
then rebuild and verify the original ID is reconciled without another execution.
Also test persistence failure before dispatch, lost acknowledgement, mismatched
acknowledgement, concurrent duplicate admission, conflicting input, repeated
tracking registration, and explicit rerun. Use fault injection, not sleeps or
prompts as evidence. Any source guard stays narrowly scoped and does not ban
legitimate provider correlations.

## Slice 3: Public Vocabulary, Artifacts, And Integration

- Keep one public job vocabulary: `queued`, `running`, `succeeded`, `failed`,
  `cancelled`. Define each execution adapter's translation once and test it.
  Do not treat engine completion as proof of durable result publication.
- Preserve meaningful internal engine states. Remove redundant public aliases
  and duplicate mappings, not every occurrence of the word "run".
- Make screening artifacts agree with the chosen public result vocabulary and
  update producer/importer together. Change the artifact schema/version if its
  closed status contract changes; do not retain the retired spelling on read.
- Replace the `backtests/` artifact object prefix with neutral `jobs/`, and rename
  `RUN_PREFIX` accordingly. Update store paths, artifact URIs/SAS generation,
  importers, UI fixture paths, smoke tooling, and affected deployment rules.
- Replace retired `run_id` keys in digest payloads with `job_id`, defining a new
  digest contract/version where bytes change. Update all corresponding Rust
  implementations and test vectors together; do not silently change a hash
  contract under its old version. Keep meaningful content/stream digest names
  unless their purpose actually changes.
- Remove `configuration.pop("run_id", ...)`; strict validation rejects obsolete
  configuration fields. Remove active documentation and fixtures describing
  retired keys, prefixes, and public status aliases.
- Update affected architecture documents, folder READMEs, runbooks, and generated
  wire artifacts. Do not rewrite historical verification records or the previous
  implementation's test-count evidence.

Acceptance: backend/UI artifact links agree; failed jobs with no artifacts
render correctly; old public keys/statuses/prefix assumptions are absent from
active paths; digest test vectors agree across implementations. Use new jobs
for smoke checks rather than translating old results.

## Ownership And Verification

Run slices sequentially because persistence, shared tracking models, and
submission callers overlap. Within a slice, Rust/Python/UI work may proceed in
parallel only with exclusive file ownership. The coordinator owns shared
entrypoints, instructions, architecture, generated schemas, and integration.

Owners run focused checks. After all three slices freeze, the coordinator runs
one integration pass: `uv lock --check`, repository Ruff lint/format, `ty`, full
pytest including `tests/thesis/agent/runtime/test_delivery_architecture.py`,
affected Rust workspace fmt/clippy/tests, and UI generation/check/tests/build.
Use the canonical commands in repository instructions; never co-locate Rust
tests in source files. Allow one full rerun only after fixing an integration
failure; isolate hangs with a hard timeout instead of rerunning unchanged suites.

Restart affected local backends and smoke one new job through submission,
status, artifacts, and report, plus a metadata-only failure. Clean only the
obsolete job/result stores needed for the prefix/schema change; preserve market
data, authored programs, unrelated chats, and trading safety state. No deployed
resource deletion is part of this implementation without explicit authorization.

Done means the code, instructions, and behavior tests agree on responsibility
and recovery, failures do not fabricate artifacts, public vocabulary is coherent,
and exactly one execution ID remains end to end.
