# Asynchronous agent completion — 2026-10-28

## Delivered behavior

- Removed the synchronous `job_waiting.py` helper and its job-status polling loop.
  Program runs submit all validated batch points and return retained identities
  immediately. Pending and uncertain admissions keep their original IDs.
- Agent submissions retain their trusted conversation recipient before external
  dispatch. Harness terminal publication commits a compact completion event in
  the same conditional write as the authoritative result. Finite workers POST
  that event to Thesis; existing control recovery retries unacknowledged event
  delivery. It never resubmits execution or invokes job-status/result tools to
  wait. Only HTTP 204 acknowledges durable consumption.
- Completion intake resolves the exact retained ID, rejects conflicting terminal
  evidence, and delivers through canonical conversation intake with one stable
  message identity. Failure, cancellation, duplicate delivery, partial batch
  results, completion before admission, arrivals during model output and late
  cancelled-child evidence have behavior coverage.
- Long validation tools use the existing fenced durable work queue. The agent
  may continue another tool or answer input while validation runs, or end its
  response and resume when the correlated result arrives. Interrupted attempts
  are reported without replay; queued work survives worker replacement. Caller
  interruption before queue admission settles the outstanding call visibly.
- Inline and deferred dispatch share pure result normalization and the same
  provider projection. Pending returns are derived from the full scoped
  transcript, including runtime-authored pending/completed job identities.
- The MCP client now holds its lock only while allocating a request ID, rather
  than across the transport exchange. A behavior test executes another MCP call
  while validation remains blocked at the external boundary.
- Updated concise prompts, architecture, module inventories, generated HTTP/UI
  contracts and local/Azure completion configuration. The previous context,
  SDK paging, read diagnostics, receipt projection and protective-stop fixes
  remain in place. Look-ahead work was not expanded.

## Contract and removal disposition

No workflow phase, gate, wait tool, alternate scheduler, provider-native inbox or
second incoming writer was introduced. Delivery retries concern committed
messages, not execution waiting. The optional terminal outbox and optional
conversation recipient have one current implementation; jobs without a retained
recipient are acknowledged without inference or a model wake. The configured
completion URL must reach the Thesis deployment owning those submissions.

Synchronous wait/deadline acceptance tests were removed as superseded by the
asynchronous workflow and HTTP behavior suites. Changed fake-call/keyword wiring
assertions were removed; real workflow tests retain program identity, attribution,
window and batch behavior. The normalization fault test now returns genuinely
unserializable tool evidence rather than monkeypatching an internal function.
The durable provider-recovery test now parks for actual asynchronous validation
before submitting its study. Historical verification records are unchanged.

## Verification

- `uv run --extra dev pytest -p no:cacheprovider -q --durations=15`:
  **1,798 passed, 1 skipped, 176.86 seconds**. The existing real-Cosmos latency
  acceptance remains opt-in. This includes the mandatory delivery architecture
  guard, REQ-DEL behavior suites and execution identity/recovery tests.
- Initial integration run: 1,796 passed, two outdated tests failed, one skipped,
  205.03 seconds. After correcting those tests, the single final integration run
  above passed within the three-minute budget. No third full run was made.
- Repository Ruff lint, Ruff format check, ty, lockfile, LF and diff checks passed.
- Focused native verification: **25 tests passed** across API completion delivery
  (including 200/503 rejection and 204 acknowledgement), execution-pool ownership
  and interruption, durable result/outbox publication, duplicate admission and
  storage ownership. API test binaries were linked with native library search
  paths obtained from the installed Visual Studio environment; repository build
  configuration contains no machine-specific library override.
- Affected Rust Clippy checks and workspace Rust format checks passed.
- Bicep compilation passed; existing unused-parameter/nullability warnings remain.
- Regenerated backend wire artifacts. UI type checking still reports the existing
  `scopeEntries` declaration-order errors in `ChatSurface.svelte` plus existing
  CSS warnings; that component was not changed in this iteration.

## Local rebuild and restart

The canonical local launcher rebuilt optimized Harness/worker binaries and checked
its ingestion worker build, then restarted Harness and Thesis. The detached
operational wrapper loaded the installed Visual Studio build environment first.
Both `/health` endpoints returned `status: ok` after restart. Release binaries
were rebuilt at 17:46 local time. Listener owners: Harness 1236; Thesis 35176.

Live smoke verification confirmed the new completion route/schema, HTTP 401
without credentials and HTTP 204 for an authenticated, untracked terminal event.
That event had no submission recipient and created no job or chat evidence.
No live backtest, strategy search, commit, push or Azure deployment was performed.

Evidence: `.local/reviews/async-completion-python-final.log`,
`.local/reviews/async-completion-python.log`,
`.local/logs/restart-async-completion.log`; focused native command output is
retained in this chat's tool history.
