# Workspace deletion verification — 2026-10-12

Workspace deletion is available in the switcher and picker. The API durably
marks all data owned by the workspace ID for deletion, hides its catalog row,
rejects workspace routes and runtime conversation activity, and starts existing
conversation cleanup. Startup reconciles interrupted cleanup from retained
markers. Repository, Blob and other non-conversation storage remain marked for
later physical erasure; their automatic collector is not implemented.

## Focused evidence

- `tests/thesis/http/test_workspace_deletion.py`: all five behavior cases passed
  in the integration attempt. Coverage includes both API prefixes, archived and
  active chats, retained conversation handles, active workflow access, unrelated
  workspace isolation, ID reservation, local restart, hosted unknown outcomes,
  owner authorization, CSRF, and authorized deletion retries.
- `uv run --extra dev pytest -p no:cacheprovider tests/thesis_runtime/test_delivery_architecture.py tests/thesis_runtime/test_arrival_reconciliation.py tests/thesis_runtime/test_session_input_snapshot.py tests/thesis_runtime/adapters/providers/test_session_adapter.py tests/thesis_runtime/conversations/test_conversation_deletion.py`:
  **134 passed in 5.69 seconds**.
- `npx playwright test workspace-deletion.spec.ts --max-failures=1`, from
  `src/ui`, against the running local UI and Cosmos-backed API: **2 passed in
  20.2 seconds**. Tests create their own workspaces and delete only those.
  The same two flows passed against an isolated in-memory API in 4.4 seconds.
- `npm run build`, from `src/ui`: passed. The first browser attempt overlapped
  the build's generated-file reloads; rerunning after the build completed passed.
- Focused Ruff lint, Ruff format check and ty checks for the five changed Python
  source files and the new HTTP test file: passed.
- The running API previously advertised only GET for
  `/v1/workspaces/{workspace_id}`, explaining the operator's 405. Restarting only
  the Thesis API exposed GET and DELETE; the live browser flows above verified
  successful deletion afterward.

## Repository integration findings

One integration pass ran after source edits froze:

- `uv lock --check`: passed.
- `uv run --extra dev ruff check .`: passed.
- `uv run --extra dev ruff format --check .`: one formatting failure outside this
  change in `tests/thesis_runtime/adapters/providers/test_openrouter_transport.py`.
- `uv run --extra dev ty check`: five diagnostics outside this change, in
  `tests/thesis/http/test_stateless_ownership.py`,
  `tests/thesis/knowledge/test_collections.py`, and
  `tests/thesis/knowledge/test_wiring.py`.
- `uv run --extra dev pytest -p no:cacheprovider`: collected 1,696 cases and
  reached 53% without reported failures before being stopped at the required
  180-second ceiling. All five new workspace deletion cases passed. No unchanged
  full-suite rerun was attempted. The file active when stopped was isolated with
  a hard 30-second timeout:
  `uv run --extra dev pytest -p no:cacheprovider tests/thesis/strategies/test_artifacts.py`
  passed its one case in 16.80 seconds.
- `npm test`, from `src/ui`: 286 passed, one skipped, one unrelated failure in
  `src/tests/phase-run-attempts.test.ts`; its API mock omits `backendKind`, which
  the harness execution card's existing job observer reads.

Files outside this task's scope were left unchanged. The integration results do
not certify a clean full repository suite or physical non-conversation erasure.
