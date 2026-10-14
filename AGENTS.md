@.github/instructions/copilot-instructions.md
@.github/instructions/repository-organization.instructions.md
@.github/instructions/source-documentation.instructions.md
@.github/instructions/market-lake-local.instructions.md
@.github/instructions/thesis-workflow-development.instructions.md
@.github/instructions/agent-session-delivery.instructions.md
@.github/instructions/testing-strategy.instructions.md

## Agent Session Delivery Contract

Every coding agent changing Thesis agent, provider, chat, workflow, UI, or
Thesis test code MUST read and follow
`.github/instructions/agent-session-delivery.instructions.md`. Option 1 requires
one neutral session inbox, one canonical incoming writer, and one pure provider
projection shared by live execution and replay. Provider-native roles MUST NOT
become session message types or independent delivery paths.
`Conversations.deliver_session_message` and handle delegation converge on
`thesis_runtime/conversations/incoming.py::write_incoming`; tool results use the injected inbox
persistence port, not a native-history append.

The root reference loads this contract for agents reading `AGENTS.md`; the
instruction file's `applyTo` globs select it in supporting coding tools.
The `@` reference depends on loader support; unsupported loaders MUST read the
referenced file directly. Instructions cannot guarantee obedience or honesty by
every LLM or instruction loader.
The integration owner MUST retain and run executable architecture and behavior tests,
including `tests/thesis_runtime/test_delivery_architecture.py`, and MUST
NOT treat prompts or documentation as proof of implementation compliance.
The source guard is mandatory and MUST fail for missing required modules, with
no hidden optional bypass. `REQ-DEL` in the runtime architecture names the
delivery assertions and verification paths; the coordinator owns test edits and
integration verification.

## Testing Strategy Contract

Every coding agent adding, changing, or deleting tests MUST follow
`.github/instructions/testing-strategy.instructions.md`. The repository is
behavior-first: verify behavior at the highest practical boundary, and allow a
unit test only for pure low-level logic in the documented categories. The full
suite MUST finish in under 3 minutes; correct-but-expensive tests that cannot
fit are deleted or converted off their expensive boundary, not kept behind a
marker. Mandated guards and named behavior suites in the other instruction
files remain required and take precedence over any pruning rule.

## Harness Job Identity Contract

Every coding agent changing harness, Thesis submission, job tracking, or job
report code MUST follow the "Execution Job Contract" in
`.github/instructions/copilot-instructions.md` and, for workflow code, item 11
of `.github/instructions/thesis-workflow-development.instructions.md`. One
execution has exactly one `job_id`; submission composition allocates and
durably retains it before dispatch, harness admission owns join/conflict,
client adapters own acknowledgement validation, and callers own waiting.
Unknown outcomes are reconciled by the retained ID, never auto-replayed.
Instructions and prompts are not proof of compliance: the integration owner
MUST retain and run interruption, acknowledgement-mismatch, and
duplicate-admission behavior tests.

## Current Contract Policy

Follow "Current Contracts and Data Cutovers" in
`.github/instructions/copilot-instructions.md` for every contract change. Keep
one current contract and implementation, updating producers, consumers, tests,
fixtures, and active documentation together. Obsolete development data is deleted
by default within the authorized scope; necessary preservation uses a bounded
one-time conversion outside the runtime, with the converter removed afterward.
Readers may reject outdated data, never infer or upgrade it. Old data, callers,
and tests do not authorize compatibility branches or version-selected behavior.

## FCA Execution Policy

- Never assign the same file to concurrent agents. Treat shared entrypoints,
  provider clients, models, project metadata, and workflows as
  integration-owned unless ownership is explicitly partitioned.
- Subagents run focused checks only. The coordinator owns repository-wide
  verification and runs it once after edits freeze.
- Permit one additional full verification run only after fixing a failure from
  the first integration run. If a suite hangs or times out, isolate the test
  with a hard timeout instead of rerunning the unchanged suite.
- Do not start work beyond the scope the user approved.
- Remove superseded behavior within the approved scope under the Current
  Contract Policy above; do not infer preservation requirements.
- Do not rewrite historical verification records solely because later tasks
  change the repository-wide test count.
- Use `ruff` for all lint and format checks and `ty` for all type checks. Do not
  use MyPy, pyright, flake8, pylint, black, or another lint/type/format tool.
- Run Python verification through `uv` from the repository root using the
  canonical commands in `.github/instructions/copilot-instructions.md`. Never
  invoke `.venv` interpreters directly.
- Treat `.github/architecture/*.md` as the source of truth for repository
  structure, package boundaries, and practices. Update the affected
  architecture document whenever a task changes package layout, public
  facades, entrypoints, dependency direction, or deployment shape.
- Treat architecture-documentation updates as integration-owned unless a task
  explicitly assigns that file.
- Explore the repository directly with focused file and content searches.
- NEVER co-locate Rust tests inside a `src/**/*.rs` file. Every `#[cfg(test)]`
  test module lives under the package's own `tests/` directory — a peer of
  `src/` and `Cargo.toml` inside that crate. Do not add `#[cfg(test)] mod tests`
  to a source file. See `.github/instructions/rust.instructions.md` for the
  integration-vs-unit placement rule.
- NEVER use workflow gates, gate tools, or gate events (`workflow.gate.*`). All
  workflow gates, gate resolution schemas, and gate cards are permanently
  removed and prohibited. Operator interaction is conversational and
  agent-first: a phase agent asks from inside its own node with the wrapped
  `request_workflow_input` tool (orange request box on that agent, answer
  accepted for that agent), and advances only by calling
  `request_phase_completion` after the operator accepts. The engine never
  authors, relays, or pauses for operator input, and nothing percolates to the
  parent conversation. The operator always responds via standard chat input.
  See `.github/instructions/thesis-workflow-development.instructions.md`.
