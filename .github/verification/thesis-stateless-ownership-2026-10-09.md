# Thesis stateless ownership implementation verification — 2026-10-09

The operator authorized the first architecture repair batch and explicitly
included stateless execution ownership. Changes are local; no deployment or
commit was performed by this task. Concurrent harness and research UI changes
in this checkout are outside this batch.

## Delivered behavior

- API intake persists canonical messages and runnable references atomically.
  Separate workers execute conversation and workflow work using renewable
  claims and transactional fencing across replicas.
- Expired running attempts are interrupted without automatic replay. Durable
  workflow dispatch can be reconstructed, and stale intent epochs are rejected.
- Harness submissions retain allocated job IDs and immutable inputs before
  dispatch. Unknown outcomes remain inspectable rather than being resubmitted.
- Blocking HTTP adapters run through synchronous FastAPI boundaries. Tool
  identities are unique and schema validation uses Draft 2020-12 with local
  references and no network retrieval.
- Deployment definitions provide separate API and worker applications. Workflow
  records now share the workspace catalog partition needed for fenced writes.

## Verification

Repository checks before the final focused fixes passed: `uv lock --check`,
`uv run --extra dev ruff check .`,
`uv run --extra dev ruff format --check .`, and
`uv run --extra dev ty check`.

After the final fixes, scoped Ruff lint, format, and ty checks over `src/thesis`,
`tests/thesis`, and `scripts/local` passed. The lock check and `git diff --check`
also passed after declaring the schema dependencies.

Focused regression commands and results:

```text
uv run --extra dev pytest -p no:cacheprovider -q tests/thesis/http/test_stateless_ownership.py tests/thesis/workflows tests/thesis/cosmos tests/thesis/agent/runtime/test_delivery_architecture.py tests/thesis/agent/runtime/test_schema.py tests/thesis/chat/test_atomic_intake.py --tb=short
585 passed, 1 skipped in 35.01s

uv run --extra dev pytest -p no:cacheprovider -q tests/thesis/http/test_harness_routes.py tests/thesis/http/test_study_validation_recovery.py tests/thesis/http/test_serving_midturn.py tests/thesis/backtests/test_launch_identity.py tests/thesis/screening/test_service.py tests/thesis/workspaces/test_programs.py tests/thesis/agent/runtime/test_session_live_activity.py --tb=short
194 passed in 44.81s
```

The skip requires real Cosmos latency credentials. These tests use an external
Cosmos fake; they do not establish real multi-host Cosmos latency or availability.
They cover API replacement, worker exclusion, lease loss, interrupted attempts,
durable phase reconstruction, stale intent, delivery architecture, immutable
submission identity, acknowledgement mismatch, and unknown-outcome handling.

The canonical full Python command was attempted twice with a hard 180-second
limit, as allowed by the execution policy. The first exposed an intake lock-order
deadlock, which was fixed and verified by focused concurrent intake tests. The
second timed out around 40% with no recorded failures before timeout. No third
full run was performed. The required full-suite runtime contract is not met or
verified by this task.

The Thesis Bicep module compiled, and `thesis worker --help` passed. UI build
passed; the job-identity UI tests passed (5 tests). The full UI run reported
263 passed, 11 failed, and 1 skipped, with failures in workflow/chat rendering.
UI type checking reported a missing `total` in the research fixture and two
warnings. This batch's textarea warning was subsequently fixed. Concurrent
rendering/research changes were preserved; the UI suite is not green.

## Deployment limitation

Drain active workflows before deploying the new execution partition shape.
There is no automatic migration or effect replay. Existing old-container data
is not deleted by runtime startup. Real deployment, cross-host fault injection,
and a passing full repository run remain unverified.
