---
applyTo: "**"
---

# FCA Copilot Instructions

Use Python 3.12 with uv, Ruff, ty, and pytest. Do not use MyPy.

## Task Work

- Work only within the task's assigned files and behavior.
- Run focused tests and focused Ruff and ty checks only.
- Do not run the complete repository suite unless acting as the integration
  owner.
- Do not modify another task's verification record or mutate CLEO task state.
- Treat failures in files concurrently owned by another agent as integration
  findings, not final failures for the current task.

## Integration Work

- Wait until every agent in the wave has stopped editing, then freeze the wave.
- Run `uv lock --check`, repository-wide Ruff lint and format checks, ty, and
  pytest once. Run applicable Bicep and workflow checks in the same integration
  pass.
- Permit one final repository-wide rerun only after fixing a failure found by
  the integration pass.
- If a test run hangs or times out, isolate the hanging test with a hard timeout
  before changing code. Do not rerun the unchanged full suite.
- Keep completed task evidence unchanged when later tasks add tests. Record
  focused task evidence separately from one shared wave integration artifact.
- Reconcile CLEO and run its full validation/export checklist once per wave,
  immediately before commit or handoff.
- Do not start work beyond the user-approved wave count.
