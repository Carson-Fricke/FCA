# Thesis message send latency verification - 2026-10-12

The operator authorized the frontend and backend fixes. Existing unrelated
working-tree changes were preserved. Additional provider/schema edits landed
during integration verification; this record does not certify that changing tree.

## Delivered behavior

- Ordinary Cosmos input admission uses two point reads and one conditional
  transaction instead of chat/intake lease acquisition and release. The batch
  retains canonical input, sequence, inbox head, root bookkeeping and runnable
  work together. Worker ownership fences remain in the transaction.
- ACK, resource links, archive and deletion recompute pure updates against the
  same root ETag. Contention preserves competing admissions and lifecycle state.
- Delivery returns its committed record without another lookup. Reconciled
  session inputs are observations, not repeated writes or changed attribution.
- Root and node HTTP input accepts a stable message identity. Matching retries
  join that identity; conflicting content/actor fails. Lost transport responses
  reconcile the exact identity without automatically replaying unknown writes.
- Root and scoped composers clear immediately, and a local pending bubble is
  displayed before HTTP acceptance. POST and SSE reconcile by identity without
  fabricating a stream cursor. Failures preserve newer typing and explicit retry
  reuses the retained identity.

## Running stack measurement

Thesis was restarted with
`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/local/start-stack.ps1 -Servers thesis -Foreground`.
Its health endpoint subsequently returned `{"status":"ok"}`.
Vite served the frontend changes without requiring a restart.

Twelve POSTs against the restarted local API and real Cosmos emulator measured
100.2, 204.7, 117.5, 119.1, 120.4, 131.4, 124.7, 148.1, 143.1, 144.1, 120.3 and
106.6 ms: median **122.6 ms**, maximum **204.7 ms**. A same-ID retry returned in
107.4 ms. Earlier normal sends measured roughly 250-325 ms. This sample is not a
tail-latency guarantee or a model-execution benchmark. An isolated diagnostic
chat held its turn claim to prevent model dispatch; only diagnostic records
were removed afterward.

## Focused verification

- `uv run --extra dev pytest -p no:cacheprovider tests/thesis_runtime/test_initial_intake.py tests/thesis_runtime/adapters/cosmos/test_conversation_scope.py tests/thesis/http/test_stateless_ownership.py -q`: 29 passed before the concurrent provider composition change.
- `uv run --extra dev pytest -p no:cacheprovider tests/thesis/http/test_application_parity.py tests/thesis/http/test_chat_routes.py -q`: 32 passed after updating committed-record expectations and using a valid workspace ID in the missing-chat test.
- `npm --prefix src/ui test -- src/tests/chat-host-render.test.ts src/tests/chat-host.test.ts src/tests/chat-scroll.test.ts`: 39 passed.
- `npm --prefix src/ui test -- src/tests/node-send-conflict.test.ts src/tests/workflow-live-render.test.ts`: 65 passed.
- `npm --prefix src/ui run test:e2e -- e2e/chat.spec.ts --grep 'pending send'`: Chromium flow passed in 6.8 seconds, with HTTP acknowledgement deliberately delayed, one message bubble, immediate clearing and preservation of newer typing. It dispatched no model work.
- Focused Ruff checks passed before concurrent source edits. The lock check passed.

## Integration findings

The first repository Python run produced 1646 passes, 5 failures and 1 skip in
223.83 seconds. Its failures were corrected: committed-record test expectations
and an invalid single-character workspace ID in a missing-chat fixture.

The permitted final run produced 1617 passes, 34 failures and 1 skip in 188.45
seconds. New provider/schema changes during verification caused provider `.model`
setup failures, altered schema-error expectations and writer-error behavior.
The mandatory delivery architecture suite was retained and run; its pure-adapter
import guard fails on the new `adapters.providers.families.base` import. A focused
delivery/race rerun consequently produced 82 passes and 19 failures, including
that guard and provider setup failures. These integration findings remain for
the concurrent change owner; no clean full-suite result is claimed. Both full
runs also exceeded the repository's three-minute budget.

The final lint/type pass found concurrent provider/schema formatting/import
issues and a type error in `adapters/providers/families/deepseek.py`.
The full UI suite initially produced 280 passes, 3 failures and 1 skip. Two
failure-draft/send-completion timing assertions were corrected and their focused
suites passed; an unrelated phase-attempt API mock still lacks `backendKind`.
`npm --prefix src/ui run check` reports 9 existing errors and 2 warnings in
research plot/fixture files and `SketchToolCard.svelte`, outside this change.
