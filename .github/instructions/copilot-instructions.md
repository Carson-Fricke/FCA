---
applyTo: "**"
---

# FCA Copilot Instructions

<!-- markdownlint-disable MD013 -->

Use Python 3.12 with uv, Ruff, ty, and pytest. Do not use MyPy.

## Remove Superseded Behavior

- Remove superseded behavior within the approved scope unless the user explicitly
  requests preservation. There is no implicit "legacy" compatibility requirement.
- Old documentation, persisted formats, existing callers, and tests do not authorize
  preservation. Update affected callers and active documentation instead of adding
  compatibility adapters, aliases, fallbacks, or migration paths.
- Tests must verify the approved current contract. Remove or replace tests whose
  requirements are superseded; do not introduce production interfaces solely to
  satisfy their setup. Retain coverage of current behavior and genuine regressions.
- If removal exposes a scope or data-impact decision that needs authorization,
  surface it to the user rather than silently preserving behavior or deleting data.

## Execution Job Contract

Applies to harness-backed work: backtests, studies, screening, paper, and live
sessions. One execution has exactly one `job_id`; do not reintroduce `run_id`,
execution `study_id`, `tool_job_id`, report/backtest ids, or operation-level
correlation ids for the same operation.

- Submission composition allocates the ID once and durably retains the
  recoverable submission reference, with immutable input/program-version
  attribution, before any external mutation. Engines, workers, importers, and
  renderers never allocate a replacement.
- Harness admission owns uniqueness: the same ID with the same accepted input
  joins; the same ID with different input conflicts; terminal results are never
  overwritten. A client-side lookup may optimize a join but is not admission
  authority.
- Client adapters own acknowledgement validation; missing or mismatched ids are
  protocol failures. Never adopt another id or infer one from aliases or prose.
- Transport interruption after possible dispatch is unknown outcome, not
  absence: inspect and reconcile the retained ID. Never auto-replay or allocate
  a replacement.
- Resume reuses the durable submission reference even before result or trial
  evidence exists. An intentional new execution allocates a fresh ID.
- Waiting is caller-specific: HTTP returns acceptance, deterministic code may
  wait, agents inspect conversationally. Do not add a universal
  submission/polling service or move polling policy into the workflow engine.
- Thesis tracking and harness execution share the `job_id`; non-harness tool
  jobs are explicitly distinguishable from harness executions. Job events may
  be correlated by the same ID; provider tool-call/message correlations remain
  separate.
- New submission paths require interruption, mismatch, and duplicate-admission
  behavior tests. Instructions and prompts are not proof of compliance.

## Required Tooling

- Lint and format with `ruff` only. Do not introduce or use `flake8`,
  `pylint`, `black`, `isort`, or any other formatter/linter.
  - Lint: `uv run --extra dev ruff check <paths>`
  - Format check: `uv run --extra dev ruff format --check <paths>`
- Type-check with `ty` only. Do not use `mypy`, `pyright`, or `pyre`.
  - Type check: `uv run --extra dev ty check <paths>`
- Do not add a parallel MyPy configuration, lint configuration, or type-check
  configuration to `pyproject.toml`.
- If a check appears to require another tool, stop and surface it as an
  integration finding instead of substituting a different tool.

## Canonical Commands

Run every Python verification command through `uv` from the repository root.
Do not call `.venv\Scripts\python.exe`, `.venv/bin/python`, or any other
virtual-environment interpreter directly, and do not rely on ambient
shell activation. Use `uv run` for commands and `uv sync` only for explicit
environment setup.

| Purpose | Command |
| --- | --- |
| Install/update the dev environment | `uv sync --python 3.12 --extra dev` |
| Verify the lockfile is current | `uv lock --check` |
| Repository Ruff lint | `uv run --extra dev ruff check .` |
| Repository Ruff format check | `uv run --extra dev ruff format --check .` |
| Focused Ruff lint | `uv run --extra dev ruff check <paths>` |
| Focused Ruff format check | `uv run --extra dev ruff format --check <paths>` |
| Type check | `uv run --extra dev ty check` |
| Focused type check | `uv run --extra dev ty check <paths>` |
| Full test suite (integration owner) | `uv run --extra dev pytest -p no:cacheprovider` |
| Focused tests | `uv run --extra dev pytest -p no:cacheprovider <paths>` |
| Build a wheel for packaging checks | See the wheel build command below |
| Run a published product entrypoint | `uv run --extra dev lake --help` |

Use this wheel build command for packaging and entrypoint checks, replacing
`<temporary-dir>` with a transient output directory:

```powershell
uv build --python 3.12 --wheel --force-pep517 --out-dir <temporary-dir> --clear --no-create-gitignore .
```

Use repository-relative paths in every command and recorded command string.
Prefer `uv run --extra dev` for verification after `uv sync --locked --extra
dev`; use `uv run --python 3.12 --extra dev` only when explicitly installing
or bootstrapping the environment.

## Testing Strategy

Read and follow `.github/instructions/testing-strategy.instructions.md` for
every test change. Behavior tests at the highest practical boundary are the
default; unit tests are limited to the documented pure-logic categories. The
full suite MUST finish in under 3 minutes; expensive tests that cannot fit are
deleted or converted, not retained behind a marker. Markers `slow` and `e2e`
remain for local filtering. Focused runs:
`uv run --extra dev pytest -p no:cacheprovider -m "not slow" <paths>`.

## Architecture Documentation

For Thesis agent, provider, chat, workflow, UI, and test changes, read and follow
`.github/instructions/agent-session-delivery.instructions.md`. It defines the
required Option 1 delivery boundaries and executable architecture guard.

- Update the affected architecture document in the same change whenever work
  adds, removes, renames, or repurposes a package or capability directory, or
  changes public facades, entrypoints, dependency direction, deployment, or
  runtime shape. Architecture reads are optional reference, not required
  orientation.
- Keep architecture updates factual and current. Update the `## Status`
  section when a planned capability becomes implemented. Do not describe
  planned behavior as implemented, and do not leave stale package or
  capability tables after a move.
- Preserve historical verification records as immutable evidence. Update
  architecture documentation and active imports instead of rewriting completed
  task evidence.

## Task Work

- Work only within the task's assigned files and behavior.
- Run focused tests and focused Ruff and ty checks only.
- Explore with focused file and content searches, then read files directly.
- Do not run the complete repository suite unless acting as the integration
  owner.
- Use the Canonical Commands table above instead of ad-hoc interpreter or
  script invocation.
- Treat architecture-documentation updates as integration-owned unless a task
  explicitly assigns that documentation.
- Do not modify another task's verification record.
- Treat failures in files concurrently owned by another agent as integration
  findings, not final failures for the current task.

## Integration Work

- Wait until every agent has stopped editing, then freeze.
- Run `uv lock --check`, repository-wide Ruff lint and format checks, ty, and
  pytest once. Run applicable Bicep and workflow checks in the same integration
  pass.
- Permit one final repository-wide rerun only after fixing a failure found by
  the integration pass.
- If a test run hangs or times out, isolate the hanging test with a hard timeout
  before changing code. Do not rerun the unchanged full suite.
- Keep completed task evidence unchanged when later tasks add tests. Record
  focused task evidence separately from one shared integration artifact.
- Do not start work beyond the approved scope.

## Workflow Interaction & Gates Policy

- NEVER use workflow gates, gate tools, or gate events (`workflow.gate.*`). All
  workflow gates, gate resolution schemas, and gate cards are permanently
  removed and prohibited.
- Any operator clarification, review, step validation, or approval blockage
  MUST route conversationally through `_emit_orchestrator_text` and
  `SUB_AGENT_NEEDS_INPUT` (`_Fenced`), using the exact same conversational
  message pattern as hypothesis creation.
- The operator always responds via standard chat input.
