# Workspace program SDK ownership

## Change

Separated SDK identity, source response validation, reference reads and minimal
template rendering from `WorkspacePrograms`. The lifecycle coordinator now
consumes `AuthoringSdk`; application composition constructs `SdkReferences` with
the existing metadata/reference client independently of its execution client.
The SDK owner receives no lifecycle service, repository, submission store or
workflow context. Shared program kinds, failures, canonical JSON and configuration
rules have one owner in `workspaces/program_contracts.py`.

Consolidated the existing source hashing and line/token reading modules under
`workspaces/sdk/`. Removed their superseded files and updated current callers,
fixtures, installed-package smoke and architecture documentation together.
The public workspace facade continues to expose program operations and errors.
SDK construction is explicit; no constructor fallback captures the lifecycle
coordinator. Source identities, compiler/content fences, Git publication locks,
submission IDs and async completion paths retain their existing contracts.

No persisted data or model-facing tool schema changed. Execution before critique
remains prompting policy. No execution requirement or workflow gate was added.

## Verification

- Focused program, SDK, serving-composition and tool suites: **143 passed**.
  Existing behavior tests caught and verified repairs to a configuration-validator
  name collision and a remaining class-qualified window check during extraction.
- Full canonical Python suite: **1,801 passed, 1 skipped, 107.25 seconds**.
  Includes mandatory delivery architecture/arrival/audit behavior and execution
  interruption and acknowledgement mismatch coverage.
  The skip is the existing opt-in real Cosmos latency test.
- New static architecture guards protect SDK/lifecycle dependency direction;
  these are boundaries that behavior tests alone cannot establish.
- `uv lock --check`, repository Ruff lint/format and repository ty: passed.
  The initial lint pass found one relocated import out of order; its correction
  changed import formatting only. The Python suite was not repeated unchanged.
- `git diff --check` and repository LF check: passed.
- Harness duplicate-admission behavior: **1 passed**. The current-source release
  integration binary verified that identical submissions dispatch once and
  conflicting accepted input returns HTTP 409. Cargo successfully produced that
  binary but its overall command failed when Windows prevented replacing the
  running `harness.exe`; the newly built `http_parity-0a8ad0703846a7fa.exe` was
  executed directly with the exact test filter. The existing service was not
  interrupted. Compilation and test-result logs are retained separately under
  `.local/reviews/workspace-sdk-admission-release-20261029.log` and
  `.local/reviews/workspace-sdk-admission-result-20261029.log`.

## Packaging and local deployment

- Built the wheel with the canonical `uv build --python 3.12 --wheel
  --force-pep517` command. Inspection verified the SDK capability and shared
  contracts are included, with neither retired `workspaces/sdk.py` nor
  `workspaces/sdk_reference.py` present. The first build exposed a stale copy
  of the old module in ignored build outputs; that build was stopped and the
  identified stale SDK files were removed before the successful rebuild.
- Ran the CI installed-SDK smoke with locked exported requirements, the wheel,
  `uv run --isolated --no-project` and Python `-I`. The installed dependency
  graph, SDK metadata, source references and templates passed outside the
  checkout import path.
- Restarted Thesis through `scripts/local/start-stack.ps1 -Servers
  @('thesis') -Foreground`; the new process listens on port 8000 and reports
  `{"status":"ok"}`. Harness on 8001 also reports healthy. This task changed
  Python ownership/composition only, so no native service rebuild was needed.
- A read-only live MCP smoke using the production lifecycle/reference
  composition returned six complete SDK topics, an advancing token continuation
  and the expected invalid-token-budget diagnostic. No paid model invocation or
  market-data strategy execution was initiated.

Native and UI implementations were not changed by this task. Historical
verification records were preserved.
