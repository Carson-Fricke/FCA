# Thesis Cosmos Conversation Store Rearchitecture Plan

Status: implemented in the worktree. Sections 1-6 and 8-11 landed; remaining
items are listed under "Implementation Status" below. This document keeps the
original plan and the record of what landed and what remains.

## Implementation Status

Landed:

- `find_delivered` no longer scans conversation history; delivery identity is a
  deterministic point read (`src/thesis/agent/runtime/delivery.py`).
- One authoritative conversation store: messages, events, and per-chat sequence
  counters live in the workspace-partitioned `catalog` container. The
  catalog-to-timeline projection, acknowledgement patches, `reconcile_timeline`,
  HTTP repair tasks, and the `timeline` container provisioning are deleted.
- Messages and immutable first-save execution snapshots are the timeline
  documents; indexed top-level `chatId`/`conversationId`/`rootConversationId`/
  `threadId`/`seq` fields scope every conversation query.
- A message or event create commits the counter CAS and the item in one
  transactional batch (`CosmosTimeline.create_with_sequence`); duplicate
  creates resolve to the existing document.
- SSE is commit-driven: `CommitNotifier` wakes timeline readers, which snapshot
  the version before reading durable items; the fixed 500 ms poll is replaced by
  a 15 s recovery interval.
- Old conversation data was deleted in the local emulator (catalog and timeline
  containers dropped; executions retained) and a fresh-start end-to-end check
  passed: create chat, durable message with `seq`, timeline read, logical delete,
  purge of messages/events/counter, tombstone retained.
- Measured on the local emulator after the rewrite: root `send_message` mean
  177 ms, p50 178 ms, p95 222 ms (10-20 sends, warm store). The plan's
  acceptance target was p95 below 250 ms.

Remaining:

- UI pending presentation and immediate receipt merge (section 7). Commit-driven
  SSE already removes the perceived polling delay, so this is optional.
- Node resume and the chat summary still commit as separate writes; folding them
  into the same partition batch is the remaining round-trip reduction and was not
  needed to meet the latency target.
- Old `timeline` containers in deployed environments are no longer provisioned
  or read; deleting them is an operational wipe, not a code path.

Also landed after the first cutover:

- The ephemeral delta broker is deleted: `InMemoryChatStreamBroker`,
  `ChatStreamFrame`, `_DeltaSink`, `ChatSubscription`, `PROVIDER_DELTA`/
  `CHAT_MESSAGE` frame publishing, `Conversations.broker`, the `create_app`
  `chat_stream_broker` parameter, and the `ServingStack.broker` field. The only
  live streaming mechanism is durable timeline reads woken by `CommitNotifier`.
- Legacy transcript decoding is deleted: `from_record` requires the canonical
  `sessionMessage`, `decode_transcript` decodes canonical records only, and the
  native-role/old-delivery reconstruction and legacy correlation inference are
  gone. Storage convenience intake and assistant convenience output build the
  canonical envelope at the storage boundary.
- The delivery instruction and architecture documents no longer require legacy
  decode; the mandatory guard still passes unchanged.



## 1. Context

The current Cosmos conversation architecture stores chat records in the
workspace-partitioned `catalog` container and mirrors every message and
execution event into a separate `timeline` container partitioned by `chatId`.
The mirror is a durable outbox: each save writes the catalog record, appends a
timeline item plus counter CAS, then patches projection acknowledgement state.
Failures are deferred and repaired by a reconciliation scan. A send performs
roughly 20-30 sequential Cosmos operations, including a full-conversation
legacy idempotency scan.

Read-only measurement against the local emulator (97 messages) showed:

| Operation | p50 |
| --- | --- |
| Cosmos point read | ~5 ms |
| `find_delivered` fallback query | ~341 ms |
| intake high-water query | ~330 ms |
| timeline `read_after` query | ~52 ms |

The `find_delivered` fallback (`src/thesis/agent/runtime/delivery.py:143-168`)
scans all chat messages on every phase answer because a fresh `input-<uuid>`
key can never satisfy the deterministic id lookup. The SSE stream
(`src/thesis/http/timeline.py:117-140`) polls Cosmos every 500 ms per open
stream because nothing signals a committed write. These are design costs, not
Cosmos costs.

## 2. Decisions

| Area | Decision |
| --- | --- |
| Authoritative storage | Conversation records, messages, and events live in the workspace-partitioned catalog |
| Timeline | An indexed read view over authoritative documents, not a persisted copy |
| Transactions | Commit related conversation changes in one same-partition Cosmos batch |
| Idempotency | Stable request identity plus deterministic point lookup; no history scan |
| Ordering | Durable per-conversation positions and chat-wide sequences committed with the message |
| Streaming | SSE driven by post-commit commit notifications; database sequence is the only cursor |
| UI feedback | Pending send shown immediately; accepted message merged from the durable receipt |
| Existing data | Delete all old conversations. No migration, no compatibility reads |
| Legacy schemas | Delete legacy decode branches and their tests; no old data remains to decode |
| Compatibility window | None. Exactly one live implementation exists at every commit |
| Execution artifacts | Retain the independent execution store and its revision/CAS semantics |
| Audit storage | Retain content-addressed chunk storage for provider audits and large payloads |
| Verification | Focused checks per section; one integration pass after edits freeze |
| Code size | Production and combined production plus test source MUST be smaller than baseline |

Two constraints shape the design:

- Cosmos transactional batches cannot span the current `catalog` and
  `timeline` containers. Removing the mirror requires co-location in one
  partition, not wrapping the existing writes in a batch.
- Optimistic UI feedback must not present an unpersisted message as durably
  accepted. Pending presentation is distinct from the canonical transcript.

## 3. Target Storage Model

### 3.1 One Stored Message, Two Read Views

A message is stored once. The agent transcript and the UI timeline read the
same document. Documents carry indexed routing metadata outside the payload:

| Field | Purpose |
| --- | --- |
| `workspaceId` | Existing partition and authorization boundary |
| `chatId` | Root conversation scope |
| `threadId` | Root, helper, or phase conversation scope |
| `recordType` / `kind` | Record discrimination |
| `seq` | Chat-wide timeline sequence, where applicable |
| `executionId` | Event and execution filtering |
| `payload` | Existing bounded domain payload or chunk manifest |

Messages and events receive durable sequences. Mutable node, invocation, and
chat records stay mutable records and are not timeline entries on every update.
The immutable first-save execution snapshot remains where the UI requires it.

`src/thesis/cosmos/timeline.py` remains as a small query facade over the
catalog. It loses append, counter, and mirroring responsibilities.

### 3.2 Delete From The Live Architecture

- Catalog-to-timeline message mirroring.
- `timelineProjection`, `timelinePending`, and `timelineFinalized` fields.
- Projection acknowledgement patches and `reconcile_timeline`.
- Projection repair scheduling in the HTTP timeline router.
- The `find_delivered` legacy fallback scan and its tests.
- Legacy native-role and old delivery-schema decode branches, once no old data
  exists.
- Separate catalog/timeline physical cleanup paths.

User-facing logical chat deletion remains a product feature. Only the
projection-specific cleanup machinery is removed.

### 3.3 Retain Required Guarantees

Keep content-addressed chunk storage for large payloads and opaque provider
request audits. Audit chunks MUST remain durable before invocation publication
and provider execution. Keep root deletion fencing, terminal-state checks,
consumption ACK semantics, and single-process scheduling. This plan simplifies
storage mechanics, not delivery guarantees.

## 4. One Atomic Append

Replace the nested read/check/save sequence with one narrow persistence
operation representing one accepted conversation change.

Canonical incoming path:

```text
Conversations.deliver_session_message
    -> runtime/incoming.py::write_incoming
    -> atomic conversation append
    -> post-commit notification
    -> eligible worker wake
```

The append commits, as applicable:

- A new message or event.
- The chat-wide sequence increment.
- The conversation's incoming-position increment.
- A phase node transition from `needs_input` to `running`.
- The chat summary timestamp and required links.

Use existing chat and node records for sequence and consumption state where
practical. Do not add a generic transaction framework or a second state
subsystem.

Transaction rules:

- Root deletion and archive fencing happen at the authoritative mutation
  boundary.
- Node admission remains trusted runtime policy.
- Relevant root and node revisions are checked when committing.
- A rejected or failed batch exposes no partial accepted message and no
  premature resume.
- Steering effects remain installed before the corresponding wake.
- Consumption ACK remains separate from admission and covers only successfully
  used inputs after outgoing persistence succeeds.

A stale in-memory record may prepare a change but never bypass terminal-state
or ownership checks. Remove redundant reads by composing validation into the
append, not by deleting validation.

### 4.1 Idempotency And Unknown Outcomes

Add a stable client request identity to root and phase sends. Retries of the
same send reuse it.

1. Derive the durable message identity from scope and request identity.
2. Attempt the atomic append.
3. On a duplicate or uncertain commit outcome, point-read that identity.
4. Validate scope and content before returning the existing receipt.
5. Never allocate another message or input position for the same request.

No production path scans conversation history for idempotency.

## 5. Commit-Driven Streaming

Reuse and simplify `src/thesis/chat/streaming.py` into a thread-safe commit
notification mechanism. It retains no durable-looking message log and no
independently numbered replay cursor. The database sequence is the only
durable stream cursor. Notifications are wake hints, not authoritative
payloads.

SSE connection behavior:

1. Register for notifications before checking durable history.
2. Read bounded pages after the current durable cursor.
3. Drain while more items exist.
4. Wait for a commit notification or the recovery timeout.
5. Repeat with the same cursor.

Required behavior:

- Worker-thread commits notify the event loop safely.
- A commit between read and wait cannot be lost.
- Multiple commits may coalesce into one notification.
- Slow clients replay from storage; no unbounded queues.
- Reconnect uses `Last-Event-ID`.
- Deletion wakes subscribers and terminates the stream.
- Every producer uses the same post-commit notification path.

Remove the fixed 500 ms sleep loop and the two routine chat reads per polling
cycle. Keep a low-frequency recovery read, initially every 15 seconds, for a
lost notification or a commit followed by process interruption. SSE keepalive
comments require no database reads.

No Cosmos change-feed infrastructure in this project. Single-process ownership
is already required, so an in-process notification is sufficient; a change-feed
processor would add deployment, lease, and lifecycle complexity.

## 6. Service And HTTP Boundaries

Service layer:

- `Conversations` owns admission, canonical delivery composition, and wake
  routing.
- `ChatService` owns domain operations and read APIs.
- The persistence adapter owns atomic storage validation and commit.
- The notification mechanism owns only waking subscribers.
- The provider adapter stays pure.

Remove repeated root reads in nested getters and repeated node/message reads
within one operation. Do not add a global mutable-record cache as compensation.
Pass the minimum already-validated context through an operation while keeping
commit-time revision checks.

HTTP layer:

- Run synchronous persistence work off the ASGI event loop with a bounded
  thread call (`asyncio.to_thread`) around synchronous service calls in async
  handlers.
- Do not migrate to an asynchronous Cosmos SDK in this project.

This prevents sends from stalling SSE and unrelated requests. Latency
improvement comes from batching, not thread offloading.

## 7. Immediate UI Feedback

Use `src/ui/src/lib/timeline.svelte.ts` as the only accepted-message owner.

Before the POST completes:

- Show a visibly pending send.
- Preserve draft content on rejection.
- Retain the request identity for retries.
- Do not assign a fabricated durable sequence.
- Do not count the message as accepted or consumed.

After durable acceptance:

- The send receipt returns the committed timeline item, including its real
  `seq`.
- Merge that item immediately into `TimelineController`.
- Remove the pending presentation.
- Existing identity deduplication makes the later SSE delivery harmless.

Handle SSE-before-response, response-before-SSE, navigation changes, and
uncertain network outcomes explicitly.

## 8. Execution Order: Delete, Replace, Sew

No migration and no parallel old/new implementation. The order is
deletion-driven.

### Step A: Delete

Data:

- Delete all existing conversation data. Drop and recreate the `catalog` and
  `timeline` containers (or provision a fresh database) through the existing
  `ensure_database` bootstrap. No backfill, no tombstones, no alias records.
- Retain the `executions` container. Execution artifacts are workspace
  artifacts, not conversation data. Operators who want old chat history MUST
  export it manually before the wipe; the codebase carries no compatibility
  path.
- Delete provider audit chunks with the conversations that reference them.
  Chunk storage remains for future audits.

Code surfaces that exist only for old data or the mirror:

- `find_delivered` legacy fallback branch and its tests.
- `timelineProjection` / `timelinePending` / `timelineFinalized` handling,
  `reconcile_timeline`, and `tests/thesis/cosmos/test_persistence_repairs.py`.
- Projection repair task management in `src/thesis/http/timeline.py`
  (`reconcile`, `repairs`, `retry_at`).
- Legacy native-role and old delivery-schema decode branches once their
  fixtures and tests are deleted.
- Duplicate timeline container cleanup paths.

Deletion rule: delete the data and the compatibility surface first. Where
deleting a module body would leave a temporarily broken import, replace that
module in the same task, but never leave both an old and a new live path.

Contract updates for deleted legacy support:

- Update `.github/architecture/thesis-agent-workflow-runtime.md` and
  `.github/instructions/agent-session-delivery.instructions.md` to remove
  legacy-decode requirements that only old data justified.
- The integration owner updates the mandatory AST guard and named behavior
  suites accordingly. The guard MUST NOT gain hidden optional bypasses.

### Step B: Replace

- Add indexed routing metadata and the single authoritative store.
- Implement the atomic append: message/event, sequence, input position, node
  resume, and chat summary in one same-partition batch.
- Implement stable request identity and deterministic duplicate resolution.
- Replace the timeline module with a query facade over the catalog.
- Replace the broker frame log with commit notifications.
- Extend send receipts with the committed timeline item.
- Update wire schemas and fixtures for the receipt change.

Each replacement lands with its focused tests. Do not port tests that only
asserted the removed mirror behavior.

### Step C: Sew Together

- Recompose `src/thesis/cosmos/stack.py` for the single-store model.
- Update HTTP send and timeline handlers, including off-loop persistence calls.
- Update UI send pending/accepted presentation and timeline merge.
- Delete remaining dead modules, helper code, and tests.
- Update architecture documents to the implemented shape.
- Run one integration verification pass after edits freeze.

## 9. Fresh-Start Cutover

No migration tooling and no compatibility window.

1. Stop all conversation writers.
2. Delete and recreate the conversation containers as described in Step A.
3. Deploy the new code and run focused acceptance checks on a fresh chat.
4. Seed nothing. Operators start new conversations.

Rollback stance: reverting the application also reverts to empty containers.
There is no expectation of recovering pre-cutover conversations through code.
If a workspace needs history, it MUST be exported manually before cutover.

Wipe verification before accepting traffic:

- Conversation containers contain no records.
- No code path reads or writes a projection field.
- No code path scans message history for idempotency.
- No code path polls Cosmos on an idle SSE stream.
- Execution store contents are unchanged.

## 10. Testing And Acceptance

Scoped verification only during implementation. Run tests affected by the
changed section plus focused Ruff and ty checks:

```powershell
uv run --extra dev pytest -p no:cacheprovider <affected-test-paths>
uv run --extra dev ruff check <changed-python-paths>
uv run --extra dev ruff format --check <changed-python-paths>
uv run --extra dev ty check <changed-python-paths>
```

Run selected UI test files for UI changes. Re-run the delivery architecture
guard whenever canonical delivery boundaries change. No complete-suite run
after every update. The integration owner performs one final integration pass
after edits freeze, as required by repository policy.

Correctness acceptance MUST demonstrate:

- Duplicate and uncertain-outcome retries produce one durable input.
- Failed batches leave no sequence, state, or message partial commit.
- Cancellation and deletion fence unauthorized acceptance and dispatch.
- Root, helper, and phase inputs preserve identical inbox semantics.
- Output or audit failure leaves inputs unconsumed.
- SSE registration/read/wait races do not lose committed items.
- Accepted receipts and SSE merge without duplicates or fabricated ordering.
- A fresh empty store supports chat creation, sends, phase input, deletion,
  and restart recovery.

Performance acceptance, measured under concurrent phase writes and multiple
connected tabs:

- No history scan on send, ACK, or duplicate resolution.
- No cross-container mirror or projection repair.
- Ordinary acceptance uses one batch plus bounded point reads.
- Idle SSE connections perform no 500 ms database polling.
- Warm emulator POST p95 below 250 ms.
- Commit-to-SSE p95 below 100 ms on the local stack.
- Pending-send presentation appears immediately.

These are acceptance targets. Validate them separately against real Cosmos.

## 11. Code-Reduction Contract

Take the baseline from the actual worktree when implementation begins.
Unrelated changes and deletions do not count toward this project's reduction.

Completion requires:

- Production source in the affected subsystem is smaller than baseline.
- Combined affected production and test source is smaller than baseline.
- Generated code, fixtures, and documentation are reported separately.
- Required behavior coverage is retained.
- Exactly one live implementation exists; no dead compatibility branches.
- No new generic repository, event bus, cache framework, or change-feed
  service is introduced.

Expected deletion sources are projection intents and repair, mirror writes,
independent broker cursors and frame history, repeated validation reads,
legacy send scans, legacy decode branches, and two-container cleanup. The
delete-first, no-migration order increases net deletion because no migration
tooling and no compatibility reads are written at all.

## 12. Risks And Guardrails

- Deleting legacy decode branches changes a documented contract. The
  architecture document and delivery instruction updates in Step A are
  mandatory, not optional cleanup.
- The mandatory delivery guard and its named suites are integration-owned.
  Weakening them to pass a simplification is prohibited; they are updated
  only where the deleted legacy-data requirements no longer apply.
- Cosmos batch limits (100 operations, 2 MB request) bound one atomic append.
  Chunked or oversized payloads MUST keep working through manifests.
- Execution-store documents retain their own revision and ETag concurrency
  and MUST NOT be folded into the conversation store.
- Deletion is irreversible for old conversations. Confirm the import/export
  decision before Step A runs.

## 13. Explicit Non-Goals

- Migrating or preserving old conversations.
- Cosmos change feed, distributed leases, or multi-writer deployment.
- An asynchronous Cosmos SDK migration.
- A generic repository or unit-of-work abstraction.
- Rewriting provider projection, session semantics, or workflow execution.
- Changing user-facing deletion, steering, or operator policy behavior.
