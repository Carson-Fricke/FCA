# Workflow ports and routes verification — 2026-10-25

## Delivered scope

Replaced structural split phases and transition-only wiring with named required
input ports and explicit routes to node.port destinations. Selected routes support
forward branches, loopbacks and concurrent fan-out. Required inputs synchronize;
alternative producers share a port. Derived activation scopes track causal branch
coverage through partial and nested convergence without pairing a split with a
specific consumer. Conflicting inputs, stale attempts and cross-occurrence inputs
are rejected.

The generic engine under src/thesis_runtime/workflows/graph has no conversation,
agent, tool, provider, persistence or scheduling imports. The existing workflow host
binds activities and retains canonical conversation delivery, trusted completion
acceptance, revision CAS, scoped histories and durable scheduling. Only explicitly
replay-safe deterministic activities can be recomputed after interruption.

Hypothesis development is now version 6.0.0 and research investigation 4.0.0. The
research critique route feeds the three reviewer inputs directly; report collection
is an ordinary deterministic activity receiving three named inputs. Definition
HTTP/wire/UI consumers publish and render inputs/routes and pendingInputs. Accepted
history requires route/output; outdated transition history is rejected. No local or
Azure runtime data was deleted and no running service was restarted or deployed.

## Verification

Final commands ran from the repository root unless npm selects its project:

- uv run --extra dev ruff check . — passed.
- uv run --extra dev ruff format --check . — passed; 824 files formatted.
- uv run --extra dev ty check — passed.
- uv lock --check — passed.
- node scripts/ci/line-endings.mjs --fix — normalized zero files; LF checks passed.
- git diff --check — passed.
- npm --prefix src/ui run generate:wire -- --check — passed.
- npm --prefix src/ui run check — zero errors; two existing SketchToolCard CSS warnings.
- npm --prefix src/ui run test -- --run src/tests/workflow-graph.test.ts src/tests/workflow-card.test.ts src/tests/workflow-card-performance.test.ts src/tests/multi-workflow.test.ts — 42 passed.
- uv run --extra dev pytest -p no:cacheprovider --durations=15 — final run: 1718 passed, one skipped, one deprecation warning in 189.13 seconds.

The skip is the existing opt-in real Cosmos latency acceptance, requiring
FCA_LIVE_COSMOS_LATENCY=1. Delivery architecture guards, new agent-independent graph
boundary guards, interruption/acknowledgement/admission suites and current workflow
behavior tests ran in the full suite.

## Test disposition and performance

The old sibling-cross-edge rejection was replaced by undeclared-port rejection:
explicit general routing supersedes lexical split-region constraints. Split-specific
definition assertions were replaced with fan-out routes and required-port assertions.
Existing concurrent arrival, nested fan-out, branch loop, cancellation, restart,
unknown acknowledgement, immutable receipt and stale-completion behavior remains.
New pure graph tests cover forward/backward routing, conditional convergence,
required inputs, partial convergence and conflicting producers (ordering/state
machine categories). The source-cache mutation check remains an architecture guard.
No current behavior coverage was removed to reduce runtime.

The first integration run passed 1717 tests, skipped one, and took 186.90 seconds,
exceeding the 180-second ceiling. Before the one permitted full retry, immutable
node definitions were materialized once and output validation moved to record
admission/decoding instead of every history replay. Guard parsing uses a bounded
content-keyed cache while checking existence and rereading every source; mutation
checks use independent ASTs and explicitly verify changed/missing source detection.
The broad incoming-writer guard measured 1.66 seconds alone and 2.01 in the final
suite, versus 3.08 in the first integration run.

The final full run still exceeds the three-minute ceiling by 9.13 seconds. This is
an OPEN verification issue, not a passing performance claim. Largest final costs:

- Artifact publication/restart/catalog: 9.33 seconds call plus 7.56 seconds setup.
- Research local restart/idempotency: 8.20 seconds call.
- Program updates/read ranges: 7.22 seconds call.
- Sketch handoff inspection: 6.09 seconds call.

No further full-suite run was performed after the permitted retry. Historical
verification records were left unchanged. Pre-existing pending provider/research
work was retained; the full checks cover the combined working tree.
