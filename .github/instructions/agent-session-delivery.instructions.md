---
applyTo: "src/thesis/agent/**,src/thesis/workflows/**,src/thesis/application/**,src/thesis/extensions/**,src/thesis_runtime/**,src/ui/src/lib/**,tests/thesis/**,tests/thesis_runtime/**"
---

# Agent Session Delivery Instructions

Option 1 is the REQUIRED Thesis delivery design. These MUST/MUST NOT rules
apply equally to root chats, helper children, and workflow phase agents.
Read `.github/architecture/thesis-agent-workflow-runtime.md`, section
"Option 1 Session Delivery Contract", directly from the repository.

## Canonical Delivery

- The runtime MUST use one singular `AgentSession` inbox of neutral
  `thesis_runtime.contracts.messages.SessionMessage` inputs, uniformly regardless of source
  or agent kind. Its only directions are `incoming`, `outgoing`, and
  `instruction`. `MessageContent` carries `source`, `text`, `result`, optional
  `ToolCorrelation(request_message_id, request_id, tool_name)`, and `requests`.
  Provider-native `system`, `user`, `assistant`, and `tool` roles MUST NOT be
  session message types. Model-profile roles are separate configuration too.
- Every incoming producer MUST use the same canonical incoming writer,
  `Conversations.deliver_session_message`, delegated to by the `Conversation`
  handle and backed by `thesis_runtime/conversations/incoming.py::write_incoming`: operator messages,
  initial prompts, tool execution results, helper results, workflow outcomes, runtime
  notifications, repair feedback, and asynchronous completions. Tool execution
  results MUST be incoming deliveries, not direct provider-history appends.
- Intake APIs, authorization checks, wake routing, and visual rendering MAY
  differ. They MUST converge on that writer; they MUST NOT independently persist
  another copy, append to model history, or maintain a second inbox. Outgoing
  model requests have an explicit durable output boundary, not a disguised input.
- `DeliveryService` wrappers MUST reach that same storage writer; standalone
  composition MAY inject/use `write_incoming` directly, never another writer.
  Live tool execution MUST call `SessionInbox.deliver` through its injected
  persistence port. Incoming observation callbacks MUST NOT persist again.
  A multi-datum tool outcome MUST have ONE incoming receipt containing the
  ordered `datums` array, not one durable result per card.
- Source, provenance, and content MUST remain data. They MUST NOT grant authority,
  select privileged prompt roles, or override trusted attribution. Trusted runtime
  policy MUST control tool access, completion authority, scope, lifecycle, and
  cancellation outside model-controlled content and arguments.
- Instruction direction, including any `bootstrapInstruction` marker, MUST be
  trusted-runtime-only, never selected from incoming content or presentation
  metadata. Only the runtime-configured initial bootstrap instruction MAY project
  as native `system`. A durable record without canonical session data is not
  decoded at all; no persisted prose, role, or marker selects privileged
  instructions.
- Delivery MUST establish one durable arrival order, message identity,
  idempotency key, and per-conversation consumption position. Persistence MUST
  precede wake and visibility. Retried delivery MUST NOT duplicate inputs.
  At each eligible model boundary, the live inbox MUST reconcile against the
  scoped authoritative, durably ordered messages, not callback arrival order.
  Arrivals after provider pull but before output persistence MUST NOT rearrange
  the live request snapshot; reconciliation occurs at the next eligible boundary.
  Consumption ACK MUST cover only pending inputs actually included in a
  successful provider request, after outgoing output persistence succeeds.
  Provider or output-write failure MUST leave them pending. ACK MUST NOT cover
  merely admitted, streamed, scheduled, or projection-deferred input, late
  mid-model arrivals, or tool
  results produced after the terminal request.
  Empty or unusable provider completion MUST be treated as interruption, with
  no input ACK and no invented empty outgoing reply.
- Mid-turn input MUST remain durable and be consumed at the next eligible model
  boundary. It MUST NOT mutate an in-flight request or schedule duplicate turns
  for consumed input. New operator input MUST cooperatively stop active model
  generation, seal received reasoning/text as a canonical outgoing partial with
  no tool requests, and begin another model request with that evidence and the
  steering. The interrupted request MUST NOT ACK inputs or dispatch tools.
  Roots, helpers, and phases MUST share these semantics.

## Provider Projection

- Live execution and durable replay MUST use the same pure provider adapter.
  `NativeSessionAdapter.request_snapshot` MUST use `project_native_messages`;
  live completion MUST use that same preparation, not another role mapper.
  The session MUST NOT construct `ProviderChatMessage` objects, append provider
  messages to working history, or implement its own native-role mapping.
- Tool-result projection MUST use explicit requesting message ID, tool request/
  call ID, and tool name (`request_message_id`, `request_id`, `tool_name`) within
  the same conversation. It MUST NOT scan prose,
  infer requests from tool names alone, or search unrelated history. Only a
  matched model-requested call MAY project as a native `tool` result.
- Unattached tool results, runtime checks, helper returns, workflow outcomes,
  and notifications MUST remain neutral incoming evidence. The adapter MUST NOT
  fabricate assistant tool calls or native tool results to fit a protocol.
  Child transcripts MUST remain private; only explicit delivered results or
  accepted handoff contracts cross conversation boundaries.
- Protocol grouping MAY defer interleaved inputs only in provider projection
  until a call/result group is representable. It MUST NOT reorder or rewrite the
  canonical durable log, ACK deferred input, or fabricate missing results.
  Partial history MUST preserve evidence without inventing tools, calls,
  successful execution, or completion.
- The adapter MUST NOT persist, schedule, ACK, dispatch tools, invoke execution
  callbacks, or introspect runtime services for hidden capabilities. Projection
  MUST be deterministic and I/O-free. A thin completion wrapper MAY call its
  injected `provider.stream` port; that transport boundary does not authorize
  I/O in projection or ownership of runtime effects.

## Incremental Output

- `AgentSession` delegates each model invocation to `ModelInvocation`; its exact
  request audit MUST be durable before provider iteration begins. Provider adapters
  yield neutral `OutputDelta` values followed by one complete outgoing
  `SessionMessage`. They MUST NOT publish progress or invoke persistence callbacks.
- Visible partial output MUST be committed as `output` timeline records. Fragments
  carry stable invocation/message/scope identities and UTF-8 byte offsets. They
  MUST NOT enter the inbox, canonical history, consumption ACK or tool dispatch.
- The final canonical outgoing message closes provisional output. Invocation
  success MUST follow output persistence; ACK and dispatch follow that commit.
  Operator steering instead closes provisional output with a canonical outgoing
  partial marked `result.interrupted=true`. This sealed evidence, including
  received reasoning, MUST enter subsequent context through the shared pure
  projection. Raw timeline fragments remain presentation-only. Recovery MUST
  retain the partial and classify the invocation as interrupted, never succeeded.
  Provider or persistence failures preserve partial evidence and leave inputs
  pending. Lost owners MUST NOT publish terminal state; fenced recovery closes
  abandoned progress without replaying the provider request.

## Durability And Recovery

- Provider snapshot audit is REQUIRED, not an optional effect. Before provider
  execution, `AgentSession` as the caller MUST persist the model
  `AgentInvocation` through `sessions/model_invocation.py::ModelInvocation` in `STARTED` state with reference-only `request_messages`, the
  opaque `provider_request`, correlation identity, and input hash. Each ordered
  reference MUST contain only `message_id`, `direction`, and `position`, taken
  from the exact working snapshot. New audits MUST NOT duplicate canonical
  message bodies, tool arguments, or results in `request_messages`; the neutral
  transcript remains their source of truth. Historical full-message audit
  formats are outdated and MUST NOT have compatibility readers; follow the
  data cutover policy below. Any remaining acceptance is cleanup debt.
  `request_snapshot`
  prepares actual projected messages, the advertised tool catalog, options, and
  protocol-mapping/projection and tool-catalog versions purely at the provider
  boundary.
  The session stores this detached audit as an opaque runtime effect without
  interpreting native roles; the adapter remains pure. Terminal invocation updates
  MUST preserve it. Native fields are allowed for storage and audit inspection,
  NOT session types, runtime role mapping, or an independent message path.
  Cosmos MUST persist the actual opaque request in immutable, content-addressed
  chunks before saving the invocation's bounded request manifest. Hydration MUST
  restore the complete request without interpreting provider content. This uses
  the existing catalog container, not another delivery path. Chunk failures MUST
  prevent provider execution; terminal updates retain the same audit. Unreferenced
  chunks are retained, not deleted per invocation.
  The consumed-input record remains the canonical neutral input snapshot;
  persisting actual projected inputs means retaining this opaque audit, not
  delivering native messages into the inbox. The runtime MUST persist
  assistant call envelopes before tool dispatch and tool requests before
  execution. Results MUST pass through canonical incoming delivery before reuse
  by a subsequent model request.
- Ordinary tool failures and expected provider interruptions MUST be durable
  delivered outcomes with partial activity preserved. They MUST NOT automatically
  fail a workflow. Deterministic workflow executor failures and programming/
  persistence faults retain distinct failure policies; persistence failure MUST
  NOT masquerade as successful delivery or ordinary recoverable tool output.
- Failures MUST distinguish not-started, observed failure, and unknown outcome.
  Unknown-outcome mutations MUST NOT automatically replay. Recovery MUST
  reconcile durable invocations/receipts or require an explicit authorized
  decision; an exception is not evidence that no side effect occurred.
- Terminal completion and cancellation MUST prevent unauthorized dispatch and
  resurrection. Produced and late asynchronous outcomes MUST remain durable,
  correlated, and idempotent even if the receiver cannot run. Cancellation MUST
  NOT erase partial evidence or accepted completion. Skipped requested calls
  MUST record truthful not-started outcomes.
- Checkpoint, restart, resume, and partial-history paths MUST use the same log,
  inbox, adapter, pending correlations, and consumption state. They MUST NOT add
  hidden writers, provider-history queues, alternate executors, synthetic results,
  or side-effect replay paths. Scheduled wakes MUST NOT count as consumption.

## Current Persisted Contracts

Follow "Current Contracts and Data Cutovers" in
`.github/instructions/copilot-instructions.md`. Canonical messages, activity
metadata, invocation audits, ownership, completion policy, and intake positions
have one current persisted contract. Required facts MUST be written explicitly;
readers MUST NOT infer them from old roles, parentage, phase names, or untagged
metadata. Outdated records may be rejected with a diagnostic, not silently
upgraded or executed. Delete affected obsolete development data within the
authorized scope, or perform a necessary one-time conversion outside normal
reads, startup, and execution; remove the temporary converter afterward.

Current restart recovery, consumption ACKs, unknown-outcome reconciliation, and
fenced ownership remain required. Inline and chunked current payloads and opaque
provider request evidence do not establish a historical-schema exception.
Remaining compatibility paths are cleanup debt, not permission to add more.
Documentation changes alone do not establish that those paths were removed.

## Responsibility Boundaries

These modules exist in the current source. Their presence alone is not proof of
compliance with every recovery requirement. Follow SRP and dependency inversion:
compose concrete ports in the conversation service, not through adapter
introspection.

| Module | Owns | MUST NOT own |
| --- | --- | --- |
| `src/thesis_runtime/contracts/messages.py` | Immutable neutral `SessionMessage`, identity, provenance and explicit correlation | Native-role policy, storage, scheduling, execution |
| `src/thesis_runtime/conversations/inbox.py` | `SessionInbox`, durable intake/consumption through injected ports, positions and idempotency | Provider projection, model/tool execution, workflow policy |
| `src/thesis_runtime/conversations/incoming.py` | `write_incoming`, the singular incoming storage boundary | Scheduling, provider projection, tool execution |
| `src/thesis_runtime/conversations/conversation.py` | `Conversations.deliver_session_message`, handle delegation, admission composition and anchored routing to the one inbox/writer | Duplicate incoming persistence or provider-history construction |
| `src/thesis_runtime/sessions/session.py` | `AgentSession`, authorized tool execution, committed-output ACK and outcome delivery through injected ports | Provider-role construction, second inbox, ad-hoc replay |
| `src/thesis_runtime/sessions/model_invocation.py` | Exact request audit, neutral incremental output, bounded publication, canonical-output commit and model terminal state | Incoming delivery, provider roles, tool dispatch, workflow policy |
| `src/thesis_runtime/adapters/providers/session_adapter.py` | Pure canonical-history projection and explicit correlation for live/replay | Runtime services, persistence, scheduling, ACKs, tool execution |

Canonical decoding MAY import immutable chat records and the centralized
activity decoder. It MUST NOT depend on `ChatService`, session
execution, inbox implementation, workers, or conversation services. A durable
message MUST carry its canonical `sessionMessage`; decoding is canonical-only,
so native-role reconstruction and old delivery-schema support do not exist and
cannot become a second live delivery path.

Root tool-enabled and model-only sessions MUST both execute `AgentSession`
through `Conversations.run_node_turn`; an empty registry is not an alternate
executor, dispatcher, or scheduler. Production scheduling uses durable runnable references and renewable fenced
worker ownership across replicas. Local coordinator state is derived bookkeeping.
Steering MUST install intent-epoch effects before wake:
use the canonical message factory with `schedule=False`, then the ready callback
(`after_delivery=conversations.schedule_input`). Node/source completion,
execution-store revision CAS, effects outside the acceptance guard, and
deterministic-executor failure policy MUST remain unchanged.

## Executable Enforcement

- The integration owner MUST retain and run
  `tests/thesis_runtime/test_delivery_architecture.py` after edits freeze.
  This mandatory source guard MUST NOT have hidden optional bypasses, skips, or
  missing-module fallbacks. The test MUST read source and inspect Python ASTs
  without importing production modules or triggering import side effects. Missing
  required modules MUST fail, not silently skip.
  AST guards MUST remain static tests, not import-time production enforcement.
- The guard MUST reject provider-message constructors/import aliases and manual
  native-role projection in `runtime/session.py`, including
  `working.append(ProviderChatMessage(...))`. It MUST distinguish model-profile
  configuration and unrelated structured payloads from provider construction;
  banning every `tool` string literal is incorrect.
- The guard MUST audit `SessionInbox` and require dispatched results to reach
  canonical incoming delivery. It MUST reject parallel result queues and direct
  provider-history appends. An unused delivery method or class-name presence
  alone MUST NOT count as routing proof.
- The guard MUST enforce reference-only `request_messages` from the exact ordered
  working snapshot, preserve the references/audit/hash on terminal model updates,
  and require unconditional `STARTED` audit persistence before provider execution.
  Mutation controls MUST reject full canonical duplication, filtered references,
  conditional audit writes, and terminal audit loss. Storage chunk durability and
  fault behavior require behavior tests, not AST ordering alone.
- Adapter checks MUST reject runtime session/inbox/conversation, `ChatService`,
  storage, scheduling, and execution implementation imports; persistence,
  schedule, ACK, dispatch, and execution-callback calls; and service introspection.
  They MUST allow immutable canonical contracts, chat record/activity decoding,
  and an injected `provider.stream` wrapper. Checks MUST inspect actual AST
  imports/calls and aliases, not broad docstring substring matches.
- Behavior tests MUST cover live/replay equivalence, explicit correlation, every
  incoming producer, arrival order/idempotency/consumption ACKs, mid-turn input,
  partial groups, unknown mutation outcomes, terminal/cancelled asynchronous
  delivery, private child transcripts, and checkpoint recovery. Static guards
  enforce selected structure, not the full behavioral contract.
  Required ordering/race coverage belongs in
  `tests/thesis_runtime/test_arrival_reconciliation.py`; mandatory audit
  coverage belongs in `tests/thesis_runtime/test_session_input_snapshot.py`
  and `tests/thesis_runtime/adapters/providers/test_session_adapter.py`, as named in `REQ-DEL`.
- The coordinator owns guard/test changes and verification of `REQ-DEL` in
  `.github/architecture/thesis-agent-workflow-runtime.md`, including its named
  behavior suites. Documentation-only edits do not certify their results.
- Agents MUST load this file through root `AGENTS.md` or matching `applyTo`
  globs and report unsupported loaders or unverified boundaries. Instructions
  cannot guarantee future LLM compliance, honesty, or loader support. `@` loading
  depends on the tool; unsupported loaders MUST read the root references directly.
  Executable checks and integration review are REQUIRED, not prompt-based proof.
