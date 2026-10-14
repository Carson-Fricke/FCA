# Thesis runtime extraction verification — 2026-10-10

The operator authorized implementing the runtime/extension architecture. This
change establishes Python package and versioned JSON boundaries for a later Rust
service. It does not implement or deploy that service, remotely host FCA tools,
restart the local stack, or migrate existing persisted FCA links.

## Delivered boundaries

- `thesis_runtime` owns neutral sessions, conversations, subagents, tool dispatch,
  workflow execution, accepted history, durable work, ownership and adapters.
  Static guards prohibit imports from FCA and adapter selection by inner layers.
- FCA composition, prompts, retrieval policy, tools, concrete workflow behavior,
  investigations and retained submissions stay in `thesis`. Generic workflow
  metadata contains no FCA knowledge-policy fields.
- Workflow extensions exchange detached JSON; the host checks trusted execution
  identity and projected phase/epoch. Registrations pin ID and version on
  recovery. FCA activities receive scoped artifact/job capabilities instead of
  the engine or its accepted-transition writer.
- Hosted tools use neutral descriptors, out-of-band trusted context and exact
  invocation acknowledgement. Unknown outcomes are retained without replay.
- The neutral codec is independent of filesystem/Cosmos adapters. Terminal
  workflow writes converge on fenced lifecycle authority. API and worker
  ownership remain independent of replica count.
- HTTP response schemas and UI fixtures use namespaced references. FCA request
  models remain application-owned. Investigation/submission Cosmos containers
  are composed independently of runtime storage.

## Test migration

Runtime tests moved to `tests/thesis_runtime`; concrete workflow/tool tests moved
to `tests/thesis/extensions`. Mandatory delivery, interruption, acknowledgement
mismatch and duplicate-admission coverage remains executable. The source guard
still fails when required modules are missing and covers both source packages.

The fake-only `test_start_workflow_handle_carries_workflow_identity` and
`test_launch_handle_reports_running_and_keeps_the_phase_marker` tests were
removed under the behavior-first policy. Their behavior is covered by real
workflow launch/completion/recovery in `tests/thesis_runtime/test_extension_host.py`
and HTTP workflow/program-serving tests. Private-engine piping assertions were
replaced by accepted-history fold/replay and JSON-endpoint acceptance-fence
behavior. Native callback fixtures now supply extension endpoints.

Oversized Cosmos fixtures retain payloads and link documents above 2 MiB, with
explicit size assertions. Shorter fixtures and fewer longer IDs reduce repeated
normalization/serialization costs without removing chunk, audit, CAS, failure,
hydration, tombstone or corruption cases.

After the first full run exceeded the mandatory 180-second ceiling, the active
atomic-intake suite was isolated with a hard timeout: 47 tests passed in 3.19 s.
The SQLite retirement test retains its per-turn and restarted high-water/fence
assertions with 32 durable turns rather than 1,000. The HTTP catalog fixture
uses the existing in-memory Git port for program projection; real strategy
publication and workspace/program-serving Git behavior remain covered. The HTTP
factory also shares only service-independent router declarations. A behavior
test proves that two applications retain independent authentication and chats.

## Verification

Focused runtime, FCA extension, HTTP, delivery, identity and policy checks passed
during development. The single permitted full rerun after the budget fixes also
passed. Historical verification records were not revised.

- `uv lock --check`: passed.
- `uv run --extra dev ruff check .`: passed.
- `uv run --extra dev ruff format --check .`: passed, 659 files.
- `uv run --extra dev ty check`: passed.
- `uv run --extra dev pytest -p no:cacheprovider --durations=15`: **1,633 passed,
  one opt-in live Cosmos test skipped**, one Starlette deprecation warning,
  **169.02 s** (171.51 s including subprocess startup). This is below the
  mandatory 180-second ceiling. No further full-suite rerun was performed.
- Focused runtime suite: 469 passed, one opt-in live Cosmos test skipped,
  23.62 s. Focused FCA suite: 1,151 passed, 154.23 s. Final catalog/application
  fixture check after the budget fix: 14 passed, 13.94 s.
- Canonical Python 3.12 wheel build: passed. Fresh wheel inspection found 80
  runtime modules, the FCA extension endpoint and no retired runtime packages.
  The stale build cache was preserved under `.local/build-before-runtime-extraction`
  before rebuilding; it had incorrectly retained retired package paths.
- `az bicep build` for both `infra/modules/thesis-cosmos.bicep` and
  `infra/modules/thesis.bicep`: passed.
- `uv run --extra dev thesis --help` and imports of all 15 backend capability
  modules: passed. `thesis compose` is blocked by the unchanged YAML loader's
  string/boolean key comparison in `src/thesis/configuration.py`; no configuration
  compatibility shim was introduced as part of runtime extraction.
- Backend schema re-export matches `src/ui/backend-wire.json` exactly;
  `npm run generate:wire -- --check` passed.
- UI test run: 275 passed, one skipped and one failure in
  `phase-run-attempts.test.ts` because its API mock omits `backendKind`, consumed
  by the working tree's harness execution card. UI static check reports nine
  errors in `PlotPanel.svelte` and `research-contracts.test.ts` plus two CSS
  warnings in `SketchToolCard.svelte`. These research UI issues remain outside
  this runtime extraction; the changed wire consumers pass.

The local process was not restarted, no Cosmos deployment was applied and no
existing local or hosted records were purged. Previously persisted FCA link
fields are superseded by namespaced references; no migration or legacy reader
was added. Rust service implementation and actual MCP transport hosting remain
future work behind the new protocol boundaries.
