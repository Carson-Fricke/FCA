# Conversation read surfaces — October 29, 2026

## Delivered behavior

- Outline reads select root turns/requests and relevant canonical receipt headers
  in storage before paging. PostgreSQL and Cosmos project caller-selected
  metadata without transferring full result bodies. Current Cosmos chunk
  manifests are explicitly hydrated. PostgreSQL adds message and receipt type/kind
  indexes; existing canonical records require no conversion or deletion.
- One initial outline page is loaded; further pages use Load more outline. Eight
  backend/workspace/chat-scoped snapshots retain entries and cursor across visits.
- History requests up to 40 entries and applies a 256 KiB material-page budget
  over a contiguous sequence interval. Display messages advertise shortened
  evidence and separate their receipt metadata from canonical session context.
  Open provisional-output prefixes preserve their complete current UTF-8 evidence.
- Authenticated `/timeline/items/{sequence}` reads return the exact canonical
  item. Detail and SSE records replace previews by identity without moving the
  history or live-stream cursor. Complete evidence cannot be replaced by a later
  preview. Ordinary tool bodies mount and hydrate on expansion; rich result
  previews hydrate when visible. Stale detail requests abort on conversation change.
- Hierarchy listing returns flat parent-linked summaries, result availability,
  and retained result references. Recursive children and inline result bodies
  remain available through existing explicit node-detail reads.
- The backend export, generated UI types/schema, offline producers, scripted
  browser fixtures, behavior checks and architecture documentation were updated
  together. Canonical session persistence, inbox delivery and provider projection
  were not changed. No parallel stream, writer or stored read model was added.

## Verification

- `uv lock --check`: passed.
- `uv run --extra dev ruff check .`: passed.
- `uv run --extra dev ruff format --check .`: passed, 871 files.
- `uv run --extra dev ty check`: passed.
- `uv run --extra dev pytest -p no:cacheprovider`: **1,802 passed, one skipped**
  in **123.80 seconds**. Includes the mandatory delivery architecture guard and
  interruption/streaming behavior suites. The skipped case requires explicitly
  enabled real-Cosmos latency acceptance.
- Full Vitest suite: **287 passed, one skipped**. The optional live HTTP wire
  capture was skipped; its current-contract setup now reads canonical details
  separately from display history. A focused wire-contract run passed afterward.
- Production Vite build: passed.
- Eleven browser flows passed: preview and tool-evidence expansion, outline cursor
  reuse, same-conversation reselection, older-history scroll anchoring, hierarchy
  navigation, thinking-stream replay and workflow junctions.
- Wire artifacts were regenerated from the backend export and compared with
  `node generate-wire.mjs --check`; repository LF and diff-whitespace checks passed.

The initial integration test run found three streaming tests inspecting canonical
session data through history. They now verify the retained interruption evidence
through the explicit canonical detail endpoint. One permitted full integration
rerun then passed. No production behavior was weakened to satisfy these assertions.

## Local measurements

Read-only diagnostics used the existing saved conversations and an isolated API
process with no agent worker or provider. These are development measurements,
with ordinary cache and machine-load variation, not production latency guarantees.

| Read | Before | After |
| --- | --- | --- |
| Entire large-chat outline | 140 requests, about 12.4 seconds | 9 requests, 2.24 seconds in a complete read; initial UI loads one page |
| Conversation revisit | Outline restarted at zero | Cursor resumed; bounded reads completed in about 314 ms |
| Large child initial history | 6,065,024 bytes, 200 items | 143,169 bytes, 40 items |
| Large-chat hierarchy | 1,068,872 bytes | 28,007 bytes |
| Child browser snapshot | 4,421 DOM elements; 334 ms task | 795 elements; longest observed task 79 ms |

Detailed scripts, timing captures and test logs are in ignored `.local/` files
prefixed `conversation-`, `rca-conversation-after-`, and `ui-read-`. The original
RCA captures remain unchanged. No messages were sent or research jobs submitted
by these diagnostics.

## Activation

The running local API has active agent turns in workspace `4-ws`; restarting it
would interrupt those turns. Source and production build are ready. API restart
awaits the operator's activation choice, rather than interrupting running work.
