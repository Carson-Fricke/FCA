# Workflow recovery scope — 2026-10-28

## Observed failure and fix

The supplied discovery conversation received a technical recovery notice from an
internal research-investigation child and generated an unnecessary reply. The
notice was a durable input, not a UI filtering error. `supervisor_anchor` skipped
record-only workflow containers and selected an ancestor conversation.

Recovery now notifies only the child's explicit conversational return target.
A workflow record stops notification routing. Failure evidence and backed-off
continuation remain in the affected child's canonical transcript. Direct helpers
can still notify their declared parent. Explicit workflow outcomes keep their
existing delivery path. Missing, foreign-chat or self-referential return targets
do not produce notifications.

The existing incoming writer, retry policy, workflow state and data contracts are
unchanged. Existing transcripts were not rewritten, and running services were not
restarted.

## Focused verification

The new HTTP regression reproduced unwanted ancestor work admission for both a
root-owned workflow and a workflow nested under a supervising agent. Direct helper
cases passed before the fix. All four cases pass afterward, including scoped
HTTP transcript reads, durable work inspection and recovery after worker replacement.
The existing actual phase/OpenRouter recovery test now asserts no root notice.

- `uv run --extra dev pytest -p no:cacheprovider tests/thesis/http/test_recovery_scope.py tests/thesis_runtime/test_provider_recovery.py tests/thesis_runtime/test_delivery_architecture.py --tb=short`:
  **113 passed in 7.36 seconds**.
- Focused Ruff lint, format and ty checks passed.

## Frozen integration

- `uv lock --check`: passed.
- `uv run --extra dev ruff check .`: passed.
- `uv run --extra dev ruff format --check .`: passed, 841 files checked.
- `uv run --extra dev ty check`: passed.
- `uv run --extra dev pytest -p no:cacheprovider --durations=10`:
  **1,748 passed, 1 opt-in live Cosmos test skipped, 199.51 seconds**.
  The mandatory delivery architecture guard passed.
- Repository LF and Git whitespace checks passed.

The full suite exceeded the required three-minute ceiling by 19.51 seconds.
Functional verification passed; the suite timing requirement remains unmet.
The slowest call was the filesystem compaction/restart test at 12.93 seconds;
artifact publication, research persistence and workspace program tests were also
among the longest calls. Those unrelated behaviors were not changed or pruned
for this routing fix. No unchanged full-suite rerun was performed.
