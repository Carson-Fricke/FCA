# Strategy Authoring Plan

<!-- markdownlint-disable MD013 -->

## Status

Implemented authoring and publication path:

- `strategy_design` is a conversational phase with a prose-first prompt
  (`workflows/registry/hypothesis_development/prompts/strategy_design.py`);
- the workspace repository boundary is implemented —
  `thesis.workspaces.repository.WorkspaceRepositoryProvider`, with native local
  Git and the remote `GiteaWorkspaceProvider` behind it;
- `StrategyBundle`, `StrategySandboxValidator`, and the hash-verified artifact
  importer already exist.

`author_strategy` is injected only into the durable design executor. Generic
`phase_tools` grants are separate from harness MCP grants. The tool calls
`WorkspaceStrategyArtifacts`, which parses the domain-owned design schema,
validates source structure, journals accepted bytes, commits exact files under
a Git HEAD fence, and publishes through the repository provider. Completion
verifies the full persisted receipt against trusted workspace/execution scope.
No conversation internals or strategy behavior were added to the workflow core.

The Strategies HTTP surface projects verified tracked artifacts and rejects
metadata-only creation when workspace artifacts are configured. The in-memory
store remains for repository-free test compositions. Fresh clones rebuild the
projection without a checkout-local journal. Rust compilation and test
execution remain harness responsibilities; structural validation is not a
security sandbox. Later strategy backtesting and optimization work is deferred.

## Decisions of record

Operator direction, 2026-09-30:

1. **Strategies are pure Rust.** A strategy is authored against
   `harness-strategy-sdk` and JIT-compiled by the harness through
   `harness-strategy-jit`, exactly like a study or a screening program. There is
   no Python strategy surface, no JavaScript, and no second implementation
   string. This resolves the earlier doc/validator/agent mismatch (the retired
   guide said Python, `StrategySandboxValidator` requires the SDK, and the last
   design agent emitted `src/strategy.js`).
2. **Strategies are workspace artifacts.** A strategy is stored as files in the
   workspace repository — never in Cosmos, and never in a standalone strategy
   store. `strategies/<slug>/<version>/` holds the Rust source, its tests, and a
   `manifest.json`; content hashes and `parentVersion` give lineage. The
   Strategies UI is a rebuildable projection over those manifests, not a source
   of truth.

## Why

The `hypothesis_development` workflow reaches `strategy_design` after a
supported verdict, and the current phase can pass through without producing
anything the operator can see or the later phases can run. The product promise —
an agent turns a surviving hypothesis into a concrete, testable strategy — needs
three things this plan supplies: an authored artifact that is real, durable, and
inspectable; a tool the design agent can call to produce it; and a read model
the Strategies tab can render from the workspace itself.

This continues `rust-native-authoring-plan.md` (Thesis emits authored Rust
programs only) and `screener-rust-migration.md` (screener filters as authored
Rust programs) with the missing strategy path.

## Target shape

- **Authoring tool — `author_strategy`.** Granted to the `strategy_design` phase
  only. Input: the design contract (`strategyId`, `version`, `entrypoint`,
  `files{path→source}`, `tests{path→source}`, `harnessConfiguration`,
  `exitCriteria[]`, `limitations[]`) plus registry fields (`name`, `symbol`,
  `description`). The files are **Rust source against `harness-strategy-sdk`**
  (a `StrategyProgram` exported with `export_strategy_program!`). Output
  (structured): `{strategyId, version, bundleSha256, validation:{accepted,
  diagnostics}, artifactPath, status:"draft"}` — what the agent shows the
  operator.
- **Validation.** The tool builds a `StrategyBundle` (reusing
  `thesis/strategies/bundles.py` confinement and digest rules) and runs
  `validate_strategy_bundle` through the composed `StrategySandboxValidator`. A
  rejected bundle returns diagnostics and writes nothing.
- **Materialization.** On acceptance the tool writes the version directory into
  the workspace repository — confined paths only, hash-verified — and returns
  the `bundleSha256` that keys the version lineage. Idempotent on
  workspace strategy/version publication identity: identical retries recover
  the original commit; changed content requires a new immutable version.
- **Publication.** The write path is the existing workspace publication path:
  local workspaces commit through native Git; remote workspaces publish through
  `GiteaWorkspaceProvider`. Strategies do not introduce a new store.
- **Projection.** The Strategies registry is derived by scanning
  `strategies/*/*/manifest.json`. The HTTP surface serves that projection;
  it is never primary. Drafts claim no observed backtest evidence.
- **Completion validation.** `strategy_design` completion requires a successful publish. A turn
  that has not produced a validated, persisted strategy bundle cannot advance to
  `strategy_backtesting`.

## Sequencing

1. **Publish path first.** A local-only writer that turns a validated
   `StrategyBundle` into `strategies/<slug>/<version>/` and commits it, with the
   projection serving from the same tree. Prove author → file → visible in tab
   before any remote hosting.
2. **Tool + wiring.** Add the thesis-side per-phase tool grant (a `_PHASE_TOOLS`
   map alongside `_PHASE_MCP_TOOLS`, merged in `_phase_allowed_tools`), register
   the handler in `build_session_registry`, and host it on a service composed
   with the workspace's validator and repository provider. Grant it to
   `strategy_design` only.
3. **Completion validation.** Make the `strategy_design` completion validator require the
   publish result so the phase cannot pass without an artifact.
4. **Remote durability.** Let the same path flow through the Gitea provider in
   Azure mode. This is already implemented at the provider boundary and needs no
   harness change.
5. **Later.** Make `strategy_backtesting` and `optimization` real deterministic
   executors that write honest metrics and update the strategy record's status;
   `strategy_rca` loops on failure.

## Manifest shape (`strategies/<slug>/<version>/manifest.json`)

The implemented `fca-workspace-strategy-v1` manifest retains stable IDs,
content hashes, and repository-relative references:

- `strategyId`, `version`, `parentVersion`, `bundleSha256`, `entrypoint`;
- `harnessConfiguration` (symbols and window materialized from the workflow);
- `exitCriteria[]` (falsifierId, observable, condition, threshold);
- `limitations[]`; and
- trusted `workspaceId` and `executionId`, publication identity, structural
  validation diagnostics, display metadata, and the canonical bundle.

The current JIT contract accepts one self-contained `src/lib.rs`. Rust tests
under `tests/` are retained but not executed by the authoring tool.
`sourceSha256` is the separate source hash used by the harness configuration;
`bundleSha256` covers source, tests, and metadata. Each version also tracks
`.gitattributes` preserving exact bytes and `publication.json` identifying its
original commit after provider acceptance. The checkout-local journal is a retry
outbox, not an authoritative strategy database. Projection uses Git topology
for version ordering and verifies committed and materialized bytes. Parent
versions must belong to the same published workspace lineage.

## Risks

- **Language drift.** The SDK contract is the only strategy surface; any prompt
  or tool that emits non-Rust is a defect. Structural preflight checks contract
  shape, not Rust syntax, test success, or filesystem/network/process isolation.
  Native JIT compilation is not a security sandbox either.
- **Projection cost.** Scanning the strategy tree on every read is acceptable at
  workspace scale; if it is not, cache the projection, but the tree stays the
  source of truth.
- **Publication coupling.** Remote publication currently pushes accepted commits
  and does not commit arbitrary dirty files; the authoring writer must go
  through the accepted publication path rather than writing files directly.
- **Concurrency.** Strategy edits belong on investigation or execution
  branches/worktrees, with serialized canonical promotion, per
  `fca-thesis.md` "Git And Concurrency". The first slice can serialize writers
  before branching is needed.

## Out of scope

- Cosmos or any standalone strategy store.
- Python or JavaScript strategy authoring (retired).
- Harness changes: the harness receives immutable Rust source/configuration
  through its existing HTTP/MCP contract and never learns where it came from.
