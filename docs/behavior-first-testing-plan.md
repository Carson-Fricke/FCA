# Behavior-First Testing Plan

## Status And Scope

Slices 1, 2, and 4 are implemented; Slice 3 is partially implemented (shared
conftest and the stack-level e2e consolidation remain). Companion policy:
`.github/instructions/testing-strategy.instructions.md`. Objective: keep unit
tests only for low-level logic that must work and is likely to break, verify
behavior at the highest practical boundary, and remove slow build and real-git
fixtures from the default run.

This plan authorizes test changes, markers, shared fixtures, and the smallest
production injection needed to make `WorkspacePrograms` testable without real
git subprocesses. It does not authorize product behavior changes, compatibility
shims, or weakening the mandated guards and named behavior suites in
`.github/instructions/agent-session-delivery.instructions.md` and the
"Execution Job Contract" in `.github/instructions/copilot-instructions.md`.

## Outcome

Measured after Slices 1, 2, and 4:

- Default suite (`-m "not slow"`): 1,260 passed, 1 skipped in 2m00s, down from
  15m32s. CI runs it first, then the slow lane.
- Slow lane (`-m slow`): 59 passed in 7m34s, down from 92 tests in 10m30s. It
  retains the three build/native checks, the small real-git program suite, and
  the mandated `test_study_validation_recovery.py` suite. The trim reduced the
  rejected-contract matrix from 13 cases to 3, the Gitea remote-integrity matrix
  from 8 to 4, deleted overlapping clone/recovery/tamper cases, and moved
  workspace-id validation off Git.
- `tests/thesis/workspaces/test_programs.py`: 56 fast tests in 4.5s through the
  `ProgramGit` storage seam, 8 slow tests for real Git, build, and packaging
  semantics. Previously 97 tests in 528s.
- About 56 test functions (71 collected cases including parametrization) removed
  across both passes: prompt-prose, wiring, constant, and private-helper tests,
  then redundant slow-lane recovery and validation cases. Speed and boundary
  placement were the goals, not a coverage-neutral count.
- Production seam: `WorkspacePrograms` accepts `git_factory`; `GitCoordinator`
  gained `has_staged_changes` and `commit_exact_files`, and
  `GitMaterializationError` preserves the known commit on materialization
  failure. All real-Git keeper tests still pass.

## Baseline

Measured on Windows against the current working tree:

- 1,391 Python tests collected; 1,390 passed, 1 skipped (live Cosmos latency)
  in 932 s (15m32s). CI timeout is 20 minutes.
- `tests/thesis/workspaces/test_programs.py`: 97 tests, 528 s alone.
- Slowest tests: `test_study_validation_recovery.py` trials/pinning tests
  (24-26 s each), `test_artifacts.py` HTTP setup (18.7 s, 11.2 s, 10.5 s),
  `test_programs.py` native smoke (18.9 s), strategies artifact tests
  (7-17 s each), `test_gitea.py` (5-11 s each), `research/test_hypothesis.py`
  (5-12 s each).
- Roughly 100 tests spawn real `git` subprocesses; three spawn compilers or
  built binaries (`cargo check`, `uv build`, native `mcp`); the live Cosmos
  latency test is env-gated but unmarked.
- Prompt-prose and private-helper unit tests exist in several suites; the
  slowest suites are slow because a fresh real-git workspace is built per test,
  not because of the behavior under assertion.
- `slow` and `e2e` markers were unregistered and unused before this plan.

## Target Shape

- Unit tests: only the categories in the testing instruction: parsers and
  validators, canonicalization and hashing, schemas and strict decoding,
  cursor/time math, ordering and idempotency rules, small closed state
  machines, and named architecture guards.
- Behavior tests: Python HTTP through `create_app(...)` and `TestClient`;
  workflow phase contracts and durable records; a deliberately small real-git
  workspace suite; Playwright only for UI flows.
- Budget: default suite (`-m "not slow"`) within 8 minutes locally and in CI,
  with the slow lane added back in a separate CI step.

## Slice 1: Slow-Suite Extraction (implemented)

Do this first; it is the largest single win and has no policy risk.

Build and packaging checks (mark `slow` and run in a dedicated CI step; do not
delete the assertions):

- `test_programs.py::test_templates_compile_against_actual_sdk` (`cargo check`)
- `test_programs.py::test_sdk_wheel_assets_are_canonical_and_portable`
  (`uv build`, wheel inspection, interpreter subprocess)
- `test_programs.py::test_native_study_validation_and_run_document_compile`
  (built `mcp` binary smoke; currently skipif, keep as slow)
- `tests/lake/cli/test_entrypoints.py` real-binary smoke.

`test_programs.py` real-git reduction from 46 tests to about 11:

- Keep as real-git behavior, marked `slow`: commit-only-artifacts and untouched
  index, targeted update and uncommitted-edit fence, staged changes not
  overwritten, publication failure recovery, publish preserves staged operator
  edits and a deliberately staged parent commit, interrupted update
  materialization, pinned load and completion verification, historical compiler
  identity, SDK change during validation, and one representative invalid
  configuration with a HEAD-unchanged assertion.
- Convert to unit or fake-provider tests: unsafe id rejection, configuration
  override rejection, template defaults, read bounds, SDK template source
  signatures, diagnostics compaction through the public `validate` path, and the
  harness-identity/validation failure matrix (missing harness, non-accepting
  compiler, remote identity changes, unknown run outcome, SDK mismatch).
- Delete: `test_service_uses_no_private_git_methods` (source text assertion).

Production seam: `WorkspacePrograms` currently constructs `GitCoordinator`
internally, forcing real git in rule tests. Inject a repository/git factory (or
accept an in-memory repository provider) at construction; keep
`LocalWorkspaceProvider` as the production provider.

Other real-git suites: mark `slow` and trim to behavior only:

- `tests/thesis/http/test_artifacts.py`: module- or session-scope the seeded
  workspace; the 10-18 s setup cost repeats for four tests.
- `tests/thesis/strategies/test_artifacts.py`: keep publication/recovery
  behavior; merge the three clone tests; move the pure bundle-digest test to
  `tests/thesis/strategies/test_contracts.py`.
- `tests/thesis/research/test_hypothesis.py`: keep restart/isolation, remote
  publication retry, atomic-write cleanup, and clone-without-journal smoke;
  convert canonical bytes, tamper, workspace-id validation, and collision tests
  to `InMemoryHypothesisStore` or a fake repository provider.
- `tests/thesis/workspaces/test_gitea.py`: keep the remote lifecycle; move pure
  endpoint/organization validators and sanitized-error mapping off the git
  fixture.
- `tests/thesis/http/test_study_validation_recovery.py` is a mandated keep;
  only share its expensive fixture across tests.

Acceptance: `pytest -m "not slow"` completes within 8 minutes; every kept
real-git test asserts git/filesystem semantics that a fake cannot prove; the
slow lane passes separately.

## Slice 2: Prune And Convert Unit Tests (implemented)

Use the testing instruction's allow-list as the classifier. Delete or convert
the following categories, replacing each with the behavior test named or an
existing equivalent:

- Prompt prose: `test_discovery_prompt.py`,
  `test_operator_handoff_prompts.py`, the prompt assertions in
  `test_program_references.py`, `test_market_subscriptions.py`,
  `test_agent_first_loop.py`, and `test_strategy_contract.py`. Replace with one
  phase-capability behavior test per phase (tools advertised and allowed,
  completion rejected before acceptance) where no behavior test already covers
  it.
- Wiring and dependency injection: `test_workflow_runtime_wiring.py`,
  `test_backtest_observer.py`, the injected-runner test in
  `workflows/test_runtime.py`, the pass-through and private-guard tests in
  `chat/test_atomic_intake.py`, the DI assertions in `test_local.py` and
  `test_application_parity.py`, and the pass-through registry test in
  `knowledge/test_collections.py`.
- Private helpers: `workflows/test_research_models.py` (`_extract_fit`),
  the store-internals tests in `http/test_state.py` (use the state route), the
  private download/cache tests in `knowledge/test_embeddings.py`, and
  `workspaces/test_local_scaffold.py`.
- Constants and isinstance checks: the constant-set test in
  `chat/test_tracking_classification.py`, the adapter/isinstance tests in
  `http/test_auth_hardening.py`, and the builtin-version constant test in
  `http/test_workflow_routes.py`.
- Duplicate capability or tool-catalog assertions across
  `test_discovery_phase.py`, `test_agent_first_loop.py`, and
  `test_strategy_contract.py`: consolidate to one test per phase.

During implementation, produce a ledger of every deletion with its replacement
or the allow-list category it failed. Do not leave skipped or `xfail` tests.

Acceptance: material test-count reduction (expected 400-600 tests) with no
behavior currently asserted only at a lower boundary losing its highest-boundary
replacement; `ruff`, `ty`, and the focused suites pass.

## Slice 3: Behavior Harness Consolidation (partial)

- Add shared `tests/thesis/conftest.py` fixtures: app factory, deterministic
  `ChatService`, scripted provider, fake harness MCP, and workspace factory.
- Move the `Harness` fixture currently imported from
  `test_study_validation_recovery.py` into the shared support module.
- Add one program vertical-slice behavior test per program kind
  (author, validate, run, receipt and job identity) through the HTTP boundary.
- Mark `tests/thesis/cosmos/test_conversation_latency_live.py` `e2e`.
- Keep `scripts/local/run_local_stack.py` as the only stack-level smoke; do not
  reimplement it per suite.

## Slice 4: CI And Enforcement (implemented)

- Python CI: run `pytest -m "not slow"` first, then `pytest -m "slow"` in a
  step with its own timeout; keep the 20-minute job budget with headroom.
- Add `--durations=25` to the CI test output.
- Review discipline: a new unit test names its allow-list category; a behavior
  change lands with its behavior test; superseded tests are removed in the same
  change.

## Non-Goals

- No Playwright expansion for behavior that the Python HTTP boundary can prove.
- No test-count target for its own sake.
- No production refactor beyond the minimal `WorkspacePrograms` injection seam.
- No weakening of mandated guards, named behavior suites, or the Rust test
  placement rules.
