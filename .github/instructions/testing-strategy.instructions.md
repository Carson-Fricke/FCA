---
applyTo: "tests/**,src/ui/src/tests/**,src/ui/e2e/**"
---

# Testing Strategy

Scope: Python tests under `tests/`, UI component tests under
`src/ui/src/tests/`, and UI end-to-end tests under `src/ui/e2e/`. This file
defines what earns a unit test and how behavior is verified.

Mandated guards and named behavior suites in
`.github/instructions/agent-session-delivery.instructions.md`, the "Execution
Job Contract" in `.github/instructions/copilot-instructions.md`, and the Rust
placement rules in `.github/instructions/rust.instructions.md` take precedence
over this policy. Do not weaken a mandated guard to satisfy a pruning rule.

## Behavior First

The repository is behavior-first. A behavior is a caller- or operator-visible
outcome at an application boundary: an HTTP route, a workflow contract, a
workspace or program operation, a provider/MCP exchange, or a rendered UI
flow. Verify behavior through the highest practical boundary, not through
internal call graphs.

1. Every new or changed behavior MUST have a behavior test at the highest
   practical boundary. Add that test before any unit test for the same change.
2. A unit test exists only to pin low-level logic whose failure is costly and
   hard to observe through a behavior test. "It is easier to call the function
   directly" is not a justification.
3. When behavior changes, replace its test in the same change. Never keep two
   truths, and prefer deleting a redundant test over retaining it for coverage.
4. A test that breaks on an internal rename while behavior is unchanged is a
   unit test by definition. It needs one of the allow-list justifications below
   or it is deleted.

## Where Behavior Is Verified

- Python product behavior: `tests/thesis/http/**` through `create_app(...)` and
  `TestClient` with the real in-process service composition and fakes only at
  external ports (model provider, harness MCP/HTTP, lake client, Cosmos).
- Workflow behavior: `tests/thesis/workflows/**` through phase contracts,
  completion application, and durable records, not phase internals.
- Workspace/git behavior: a small `tests/thesis/workspaces/**` suite against
  the real `LocalWorkspaceProvider`; keep it deliberately small because real
  git subprocesses dominate runtime.
- UI behavior: user flows under `src/ui/e2e/**` against the local stack. Use
  Playwright only for flows that need a browser; exercise everything else
  through the Python HTTP boundary.
- Full-stack smoke: `scripts/local/run_local_stack.py` against the running
  local stack. Do not reimplement it per suite.

## What Earns A Unit Test

Only pure, deterministic logic in these categories qualifies. Category 7 is
the sole exception to the no-I/O rule; every other unit test must be I/O-free:

1. Parsers and validators for untrusted input: ids, names, prefixes, relative
   paths, intervals, timestamps, configuration normalization.
2. Canonicalization and hashing: canonical documents, content hashes, identity
   hashing, digest input ordering.
3. Schemas and strict decoding: JSON-schema argument validation, payload
   completeness rules, generated wire schemas.
4. Numeric, time, and session math with edge cases: pagination cursors,
   cutoff eligibility, calendars and interval arithmetic.
5. Ordering, idempotency, and consumption rules that are impractical to force
   end-to-end, and only where the named behavior suite cannot pin the rule.
6. Small closed state machines (lifecycle guards, caps) where each transition
   is a real edge case.
7. Architecture guards: static AST checks only where a behavior test cannot
   prove a dependency boundary. Each such test names the boundary it protects
   and belongs in the product test area, for example
   `tests/thesis/screening/test_import_graph.py`.

A qualifying unit test MUST be pure and deterministic, MUST avoid network,
git subprocesses, compilers, wall-clock reads, and environment state, MUST
target a public function or the narrowest stable contract, MUST run in
milliseconds, and MUST assert input-to-output, not call order.

## What Does Not Earn A Unit Test

- Prompt prose and descriptions. Never assert substrings of model-facing text;
  test the outcome the prompt exists to produce.
- Wiring and composition: pass-through adapters, dependency injection,
  module boundaries, constants, enums, and dataclass field mapping.
- Anything already covered by a behavior test.
- Framework behavior and library behavior.
- Build outcomes: `cargo check`, `uv build`, wheel contents, and packaging
  checks belong to CI build jobs, never a pytest suite.
- Private names and source shape: do not import `_helper` functions or assert
  source text or AST shape, except the architecture guards above and the
  guards mandated by another instruction.

## Test Doubles And Isolation

- Fake only process and external boundaries. Never fake internal domain
  collaborators to test their callers; use the real in-process composition.
- Do not replace git or filesystem behavior in a test whose subject is git or
  filesystem fencing. Keep that verification to a few smoke tests rather than
  a full suite.
- Every test must be order-independent and runnable alone.

## Speed

- The full suite MUST finish in under 3 minutes on the reference development
  machine. The budget is a hard ceiling, not a target: a test that cannot fit
  is deleted or converted off its expensive boundary, never kept behind a
  marker as an excuse.
- Targets: a unit test under 100 ms, a behavior test under 2 s.
- Markers remain for local filtering: `slow` (correct but expensive) and
  `e2e` (requires the running local stack). The default CI run executes every
  test, markers included.
- Do not create a second workspace, repository, or service when one fixture or
  session-scoped template serves the same behavior.
- Focused development run:
  `uv run --extra dev pytest -p no:cacheprovider -m "not slow" <paths>`.

## Migration Discipline

- Classify before deleting. Every removed test names the behavior test that
  replaces it or the allow-list category it failed to meet.
- Remove superseded tests in the same change as the behavior they covered; do
  not leave skipped or `xfail` tests behind.
- Do not add production interfaces, injection points, or adapters solely to
  make a unit test possible.
