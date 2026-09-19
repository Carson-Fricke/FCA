---
applyTo: "**"
---

# FCA Copilot Instructions

<!-- markdownlint-disable MD013 -->

Use Python 3.12 with uv, Ruff, ty, and pytest. Do not use MyPy.

## Required Tooling

- Lint and format with `ruff` only. Do not introduce or use `flake8`,
  `pylint`, `black`, `isort`, or any other formatter/linter.
  - Lint: `uv run --extra dev ruff check <paths>`
  - Format check: `uv run --extra dev ruff format --check <paths>`
- Type-check with `ty` only. Do not use `mypy`, `pyright`, or `pyre`.
  - Type check: `uv run --extra dev ty check <paths>`
- Build and test the native core with the pinned Zig toolchain (0.16.0).
  Zig gates run in CI alongside Ruff and ty and fail on violations exactly
  like the Python gates do.
  - Format check: `zig fmt --check .`
  - Build and test: `zig build test`
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
| Zig format check | `zig fmt --check .` |
| Zig build and native core tests | `zig build test` |
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

## Architecture Documentation

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
- Check BRAIN for prior knowledge with `cleo memory find "<topic>"` before
  exploring, then read files directly.
- Do not run the complete repository suite unless acting as the integration
  owner.
- Use the Canonical Commands table above instead of ad-hoc interpreter or
  script invocation.
- Treat architecture-documentation updates as integration-owned unless a task
  explicitly assigns that documentation.
- Do not modify another task's verification record or mutate CLEO task state.
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
- Reconcile CLEO and run its full validation/export checklist once per batch,
  immediately before commit or handoff.
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

