# Thesis Durable Agent and Workflow Runtime

The key words "MUST", "MUST NOT", "REQUIRED", "SHALL", "SHOULD",
"SHOULD NOT", "RECOMMENDED", "MAY", and "OPTIONAL" in this document are to
be interpreted as described in RFC 2119.

## Status

Current study authoring uses SDK ABI v4 and shared `MarketConsumer` callbacks.
The model declares exact `harnessConfiguration.subscriptions` for bars, quotes
and/or trades, including requirements of its owned indicators. Source declares
the same union through `subscriptions()` and `indicators()`. Compilation
validation returns effective subscriptions and rejects mismatches before
fetching. Model configuration, workspace publication and the study compiler
preserve types/symbols/intervals and the operator's sample window. Tick-only
studies do not need a bars interval. Observation-only contexts prohibit order
submission; strategy callbacks use `OrderSink`. The native built-in indicator
registry has not migrated; this shared interface applies to SDK-authored
consumers and their program-owned indicator dependencies.

This document records the current implemented runtime and the accepted design
baseline. The registered hypothesis-development topology is version `2.3.0`;
remaining normative recovery and repair-budget requirements below are not a
claim that all of them are implemented.

Workspace program authoring and reference-based workflow completion are
implemented through `workspaces/programs.py` and `agent/runtime/program_tools.py`.
Model invocations now store reference-only neutral message audits alongside the
actual projected provider request; Cosmos saves that opaque request in immutable
chunks before publishing its invocation manifest. These changes do not establish
complete crash recovery or remote deployment verification.

### Option 1 Session Delivery Contract

Option 1 is the REQUIRED delivery architecture for roots, helpers, and phases.
The neutral delivery modules now exist: `agent/messages.py` defines
`SessionMessage` and `MessageContent`, `agent/runtime/inbox.py` owns
`SessionInbox`, `agent/runtime/incoming.py` owns the storage writer, and
`providers/session_adapter.py` owns pure provider preparation. Presence of these
modules is not a claim that every recovery requirement is verified.

`SessionMessage.direction` is only `incoming`, `outgoing`, or `instruction`;
native provider roles are NOT session types. `MessageContent` contains `source`,
`text`, optional structured `result`, explicit
`ToolCorrelation(request_message_id, request_id, tool_name)`, and outgoing
`requests`. Source/provenance/content are data, not authority. Instruction
direction and any `bootstrapInstruction` marker are trusted-runtime-only;
only the runtime-configured initial bootstrap instruction MAY project as native
`system`. A durable record without canonical session data is not decoded at
all; no persisted role, prose, or marker selects privileged instructions.

Every incoming producer MUST converge on `Conversations.deliver_session_message`
through service or handle delegation, and ultimately
`runtime/incoming.py::write_incoming`. `DeliveryService` wrappers use that writer;
standalone delivery also reaches `write_incoming`, not another storage path.
Dispatched tool results use `SessionInbox.deliver` with its injected persistence
port, never native-history appends. A tool outcome containing multiple datums is
ONE incoming receipt with an ordered array; the UI expands that array purely.
Incoming observation does not write another copy.

The inbox MUST preserve durable arrival order, identity, idempotency, and
per-conversation positions. Persistence precedes visibility and wake. At each
eligible model boundary, the live inbox MUST reconcile against scoped
authoritative, durably ordered messages, not callback arrival order. Arrivals
after provider pull but before output persistence MUST NOT rearrange the live
request snapshot; they reconcile at the next eligible boundary. ACK covers only
pending inputs actually included in a successful provider request, after outgoing
persistence succeeds, not scheduling, provider/output-write failures, late
mid-model arrivals, projection-deferred input, or results after a terminal
request. Empty or unusable provider completion is an interruption: no input ACK
and no invented empty outgoing reply. Mid-turn input remains durable for the next
eligible model boundary.

Live/replay share `project_native_messages` through pure
`NativeSessionAdapter.request_snapshot`. Explicit correlations join only within
the supplied conversation scope; prose, tool names alone, or foreign history
MUST NOT associate results. Complete protocol groups may defer interleaved
evidence in projection, never rewrite the log. Partial/ambiguous groups preserve
evidence without fabricated calls/results. The adapter owns no persistence,
scheduling, ACK, dispatch, runtime callbacks, or service introspection; only its
completion wrapper calls injected `provider.complete`.

Provider snapshot audit is REQUIRED, not optional. Before provider execution,
the session as caller MUST persist a `STARTED` `AgentInvocation`
with reference-only `request_messages`, input hash, and detached opaque
`provider_request`. Ordered references contain only `message_id`, `direction`,
and `position` from the exact working snapshot; canonical bodies, tool arguments,
and results remain in the neutral transcript, not a second full canonical audit.
Historical full-message invocation audits remain readable. The opaque audit
records actual projected messages, advertised tool catalog,
options, protocol-mapping/projection version (`native-session-v1`), and
tool-catalog version prepared purely at the provider boundary. Caller persistence
owns this opaque runtime effect; the adapter remains pure. Terminal updates
preserve those fields. Cosmos forces `provider_request` into workspace-scoped,
create-only content-addressed chunks, even for small requests, before saving the
invocation manifest in the existing catalog container. `cosmos/chunks.py` uses
bounded leaves and reference pages; hydration verifies their identities, byte
count, and digest and restores the complete JSON request. Oversized record and
timeline payloads use the same chunk mechanism. A failed chunk/audit write prevents
provider execution; unreferenced chunks are retained without per-record deletion
or automatic garbage collection. Native audit fields are allowed for storage/inspection,
NOT runtime role mapping or session message types. Thus "persist actual projected
inputs before execution" means
persist this opaque audit, not create an independent native message path; the
consumed-input record remains the canonical neutral input snapshot. Outgoing call
envelopes and correlated tool invocations are durable before dispatch; results
return through incoming delivery before reuse.

Root, helper, phase, and model-only turns still execute `AgentSession` through
`Conversations.run_node_turn`; model-only means an empty registry, not another
executor/dispatcher/scheduler. Scheduling remains single-process
(`WEB_CONCURRENCY=1`). Steering uses the canonical message factory with
`schedule=False` and the ready callback
`after_delivery=conversations.schedule_input` only after intent-epoch effects
are installed. Node/source completion, execution-store revision CAS, effects
outside the acceptance guard, and deterministic-executor failure policy remain
unchanged.

Ordinary tool failures/provider interruptions remain durable outcomes rather
than automatic workflow failures. Unknown-outcome mutations MUST NOT auto-replay.
Terminal/cancelled receivers retain late asynchronous outcomes without
resurrection, unauthorized dispatch, or lost partial activity. Child transcripts
remain private. Restart/resume MUST reuse the same neutral log, inbox, adapter,
correlations, and consumption positions. Legacy native-role and old delivery
schema decoding stay confined to `messages.py` and the centralized
activity/persistence decoder, not another live path.

The complete contract and module table are in
`../instructions/agent-session-delivery.instructions.md`. Root `AGENTS.md`
references it with `@`; only supporting loaders expand that reference or apply
its exact `applyTo` globs. Unsupported loaders MUST read the references directly.
Instructions cannot ensure absolute compliance or LLM honesty. The coordinator
MUST retain and run the mandatory AST source guard and behavior suites in
`REQ-DEL`; missing required modules MUST fail with no hidden optional bypass.
Neither prompts nor static guards alone prove complete behavior.

### Implemented so far

Live tool activity is implemented for root and scoped conversations. Assistant
text and call envelopes are durable before dispatch; each tool result is durable
before the next model request. Explicit program validations and completion-time
`verify_program_reference` checks render, including failures and reused results.
Runtime
checks have their own start/result correlation without fabricated model calls.
Cards expose arguments, timestamps, duration, structured output, and failures.

Ordinary tool faults return nonterminal results to their caller. Expected model
transport/rejection and catalog failures interrupt only the affected turn;
completed activity survives, the node accepts input, and the workflow stays
running. The runtime records a local notification, not an agent-authored ask.
Unexpected programming and persistence faults outside tool execution remain
terminal. Cancellation is cooperative before requests and dispatch; synchronous
calls may finish, but cancelled nodes cannot be overwritten by late completion.
Skipped calls retain paired results without dispatching another operation.

Root and scoped composers remain writable mid-turn. Only a message POST in
flight prevents duplicate submission; failures retain drafts and show errors.
Provider replay groups matching calls/results ahead of interleaved inbound
notes, preserving durable arrival order. Scoped timeline history loads backward
with the same thread filter until exhaustion instead of truncating to one page.

Live UI delivery uses one chat timeline SSE route,
`GET /v1/workspaces/{ws}/chats/{chat}/timeline/stream`, for durable messages,
events, and execution snapshots. REST timeline history establishes the initial
`after` watermark; `TimelineStream` tails greater sequences and resumes with
`Last-Event-ID`. Legacy execution, chat-message, and sub-agent SSE routes are
removed; execution and sub-agent REST `/events` replay endpoints remain.
Frontend `timeline.svelte.ts` indexes events and snapshots by execution ID, and
the pure `workflow-live.ts` projection derives a map of live workflow states.
The singleton `effectiveExecution` is removed. Cards select their own execution
ID from that map; concurrent workflows and scoped navigation do not open
additional streams. Timeline execution snapshots are first-save state, not
current-state watermarks: all available lifecycle events replay against them,
even when their sequence precedes a late snapshot. Phase projection uses
declared workflow phases and explicit phase executors, not helpers; phase
navigation and event selection are scoped by execution ID and canonical phase.

Frontend topology reads share a 64-entry cache keyed by backend, API base URL,
workspace, workflow ID, and version, including tracked in-flight requests.
Matching explicit-version definitions have a five-minute lifetime; failed,
mismatched, and unversioned reads are removed on completion. This cache owns no
mutable execution state. Workflow-card canvas animation pauses outside the
viewport or while the document is hidden and resumes when visible.

The shared Python harness provider caches successful catalogs, not validation
results. The local `StudyValidator` delegates full-input validation reuse to the
JIT `ValidationService`, bounded to 64 entries and 4 MiB of serialized payloads.
Its opaque compiler identity
covers compiled JIT behavior, current SDK/num sources, Cargo/toolchain
configuration, relevant environment, and compiler executables. Successful probes
are cached with fresh configuration and executable checks. Changed source or
configuration invalidates both validation reuse and the actual compiled artifact
through the compiler fingerprint; identity is rechecked before caching a result.
Missing identity disables reuse. Infrastructure failures are not cached.
Cargo JSON diagnostics establish `deterministicCompilerDiagnostic`; configuration
rejections can be `cacheable` without being compiler diagnostics. MCP forwards
validator-owned `reused`, `cacheable`, diagnostics, and optional
`serverInfo.validationIdentity`, without filesystem access or compiler probes.
Python transports neither maintain a validation LRU nor infer compiler identity
or diagnostics from regexes over human-readable errors.

Concrete model-provider boundaries normalize `httpx` transport and `OSError`
failures (including SSL errors) into provider errors. `AgentSession` consumes
those errors; it does not guess infrastructure failures from exception names.

Discovery posts an early plan, authors committed studies, validates changed input,
runs the latest validated ID, and inspects artifacts. Discovery, model creation,
and strategy design share `prompts/sdk.py`: read matching SDK signatures/templates
before authoring or repairing, apply targeted patches, and ask through
`request_workflow_input` after three unsuccessful repairs. That compiler-repair
limit is prompt guidance, not a runtime-enforced repair cap or workflow gate.

The changed hypothesis-development topology is version `2.3.0`. Definition
requests can name a version; unsupported historical versions return an explicit
diagnostic rather than substituting today's legal edges. Persisted retired phase
values remain readable independently of topology availability.

The merged session registry includes `search_knowledge` and `read_knowledge`.
Shared content ships under `src/thesis/knowledge/library/`; workspace content
is confined to that workspace's repository `knowledge/` directory. Both are
searched by default. Model scope narrows `WorkflowSpec.knowledge_scope`
(`shared`, `workspace`, or `both`), resolved from trusted workflow attribution
for phase agents and helper children. Serving reads `THESIS_KNOWLEDGE_SCOPE`
when composing its workflow runtime. Results carry actual byte snapshot IDs,
page versions and source-qualified references and are always data-only.

`knowledge/embeddings.py` pins the semantic model to a commit and verifies
model/tokenizer checksums. Loading is single-flight and successful vector caches
include backend identity. Index construction and query embedding bind one
backend per search operation; primary inference failure retries the entire
operation once on the lexical fallback, never mixing vector spaces.

The durable sub-agent runtime is implemented in `thesis.agent.runtime`:

- `SubAgentRecord` and `AgentInvocation` are durable `WorkspaceRecord` types,
  persisted through the shared chat persistence port: the Cosmos `catalog`
  container is the production backend, and the in-memory and filesystem adapters
  serve tests;
- `SubAgentSpec`/`SubAgentBudget` fix the phase kind, default-deny tool surface,
  completion contract, and delegated budget;
- `SubAgentLifecycle` enforces chat/workspace scope, maximum depth, maximum
  active children, delegated-budget inheritance, and the one-shot input rule;
- `SubAgentBudget` declares construction and repair budgets, but the live
  completion path returns validation feedback without enforcing the nested
  repair-turn budgets specified below;
- `ControlToolService` is a thin adapter over the `Conversations` service: it
  records the spawn as a first-class tool invocation and delegates spawn,
  await, completion, and operator input to the conversation handles;
- `DeliveryService` adapts child results, workflow outcomes, and operator answers
  to the canonical incoming writer, then routes the anchor's wake, durable before
  routed and idempotent under retry, with `SubAgentNotifier` folded into it;
- `AgentSession` is the single durable model/tool turn loop that the chat path
  and workflow path use through `Conversations.run_node_turn`: it sends the merged catalog, persists each
  model and tool invocation as a first-class record, threads tool results back
  as neutral incoming messages, stops on a terminal control tool, and enforces a safety
  iteration cap;
- `messages.decode_transcript` is the shared neutral durable-history decoder,
  scoped by anchor: the root uses only root messages and a child only its own.
  `Conversations._persist_turn` writes outgoing envelopes only; incoming
  observations never repersist results. Runtime tool starts are activity and
  their results use the canonical incoming writer;
- starting a workflow returns its execution handle immediately and runs the
  execution detached; the outcome is delivered later at the caller's anchor
  through `DeliveryService`, so a workflow never executes inside the launching
  tool call;
- the legacy `agent/reply.py` tool-calling provider and the
  `HarnessToolDispatcher`/`WorkflowRuntime.tool_dispatcher` path are removed,
  as is `agent/session_reply.py` (`SessionReplyProvider` and its queue/adapter).
  `reply_for` and the legacy executor path are removed as well. Tool-enabled and
  model-only composition both execute `AgentSession`; model-only sessions use
  an empty tool registry, not a separate reply implementation.

The conversation abstraction (`thesis-conversation-abstraction-v1`,
REQ-034..REQ-040) is implemented in `thesis.agent.runtime.conversation` and
`thesis.agent.runtime.child`:

- one `Conversation` owns a transcript and turns for chats and sub-agents
  alike: root and child anchors execute `AgentSession` through
  `Conversations.run_node_turn`, with root activity published to the chat sink;
- the `Conversations` service is the only upstream surface — `root`, `get`,
  `children`, `deliver`, and `spawn(returns_to, spec, prompt, kind)` — and owns
  the per-conversation workers (serialize, coalesce, mid-turn drain), the
  routing rule (root to itself, record anchors durable-only, every other
  conversation on its worker), and spawning;
- per-conversation intake uses generations and monotonic durable message
  positions. The model-boundary snapshot acknowledges only observed input;
  consumed wakes do not schedule a duplicate turn and later input coalesces into
  one follow-up. `ChatThread.input_consumed_positions` persists the high-water
  mark by anchor (empty string for root). Recent-ID memory is bounded to 256,
  and paired read/ACK callbacks permit idle state retirement only after durable
  acknowledgement. Delayed positioned wakes remain fenced after retirement.
  Position zero supports historical messages without promising durable deduplication
  for them. Persistence I/O does not hold the global worker-map or intake guard;
- child `send` routes through anchored answer admission, checking scope,
  lifecycle, and operator policy before persistence and wake. It cannot bypass
  admission by writing directly to the shared chat;
- `ChildConversation` composes the return layer over an inner conversation:
  the `returns_to` anchor, `complete`/`fail`/`result`/`await_result`/
  `children`/`cancel`, and one turn-end policy. Durable `CompletionMode` selects
  `OneShotPolicy` (`complete_on_text`) or `ConversationalPolicy`
  (`explicit_completion`), independently of the input `kind`.
  `returns_to_sub_agent_id` stores the return anchor independently of parentage,
  and `phase_executor` is explicit completion authority. Phase executors always
  use explicit completion, even with one-shot input. Historical inference of
  these fields is confined to `chat/persistence.py`, not live handle reconstruction;
- the workflow engine sees only the service (`ports.conversations`) and the
  handles: phase children are spawned through `service.spawn` with the
  workflow node as their return anchor, phase results route through the
  service's delivery, and the engine builds no spawner, delivery, or scheduler
  of its own;
- phase specs are the sealed union of frozen dataclasses:
  `ConversationPhase`, `OneShotPhase`, `DeterministicPhase`, and `TerminalPhase`.
  Exhaustive dispatch keeps invalid combinations unrepresentable. Deterministic
  phases create no conversation; terminal phases have no executor or contract.

`agent/runtime/activity.py` owns the immutable discriminated `ExternalInput`,
`ModelOutput`, `RuntimeTool`, and `Delivery` contracts, their generated schemas,
and the canonical activity decoder. Transcript seeding, anchor ownership,
input recognition, and persistence consume that contract instead of independent
metadata heuristics. Every activity carries `activityKind`; a record without
canonical session data is not decoded.

The coordinator and stream brokers are process-local, not distributed ownership
leases. Serving entrypoints require `WEB_CONCURRENCY=1` and reject other values,
including values greater than one. Programmatic launchers must retain one worker.

The `hypothesis_development` workflow is a nested package
(`thesis/workflows/registry/hypothesis_development/`) with `spec`, `doctrine`,
`services`, `session_services`, `rebuild`, `piping`, `artifacts`, `state`, and
the `phases`, `contracts`, `gates`, `prompts`, and `composers` concerns behind
its public `__init__.py` facade. The retired sibling workflows
(strategy development, autonomous discovery, and the standalone RCA loop) and
their tests were removed.

`workflows/runtime.py` defines the typed `WorkflowSessionServices`,
`WorkflowSessionBindings`, and `RuntimeToolActivity` ports. The concrete
`hypothesis_development/session_services.py` owns role policy, completion
schemas, knowledge restrictions, strategy-authoring authority, and completion
validation/application. HTTP serving composes these ports, not workflow policy.
Spec-only session services remain bound when the harness/runtime is absent, so
attributed helpers retain retrieval restrictions and phase schemas. Without an
execution runtime, phase completion is denied, not silently accepted.
Runtime-owned `verify_program_reference` checks use the conversation's internal
activity writer; completion does not compile, execute, or repair program source.

### Committed Workspace Programs

`workspaces/programs.py::WorkspacePrograms` owns studies, strategies, and
indicators under `studies/<program_id>/`, `strategies/<program_id>/`, and
`indicators/<program_id>/`. Each contains `program.rs`, `manifest.json`, and,
after validation, `validation.json`. The manifest records kind/ID, dated execution
configuration with exact subscriptions, indicator IDs, and the indicator type.
IDs select confined paths; models cannot supply workspace paths or source
overrides in execution configuration. Author/update commits exact artifact bytes
through a temporary Git index under the publication lock, refusing staged or dirty
changes in those artifact files and excluding unrelated changes. Remote publication
failure retains a pinned local-commit receipt for explicit publication reconciliation,
not re-authoring or automatic execution replay.

Agent-facing read, validate, and run select the latest committed contents, never
working-tree source. Updates require the current content `expected_version`; source
patches require one exact `expected` match and paired `replacement`. Content version
hashes source and manifest file digests, including transitive indicator dependencies,
not HEAD or the validation receipt. Every dependency is read from the same selected
commit and assembled as a Rust module; changed dependencies invalidate validation.
Indicators validate through an observation-only study wrapper and cannot run alone.

Validation calls the injected harness `validate_study` or `validate_strategy`;
compilation is authoritative, with no structural-only acceptance fallback. The
committed receipt records the exact content identity, file hashes, source commit,
SDK identity, acceptance, and bounded diagnostics. The service rechecks content
and SDK identity before committing it. Runs require accepted validation matching
the latest content version and current SDK identity, then call `run_study` or
`run_backtest` with configuration assembled from committed content. Receipt commit
updates do not themselves change content identity. Unknown execution outcomes
require inspection before an explicit retry, never automatic replay.

`agent/runtime/program_tools.py` binds these operations to trusted `ToolContext`
workspace scope and the existing registry/dispatcher/inbox. Author/update alone
accept source or patches; validation/run accept only `program_id`; list tools
enumerate committed IDs with an optional prefix and bound. Program bindings
replace conflicting raw harness and legacy author registrations. Roots receive
the composed service; workflow bindings grant it only to the owning discovery,
model-creation, strategy-design, or strategy-backtesting executor, not helpers
merely inheriting tool names. The effective tool policy still filters the catalog.

`read_sdk_reference` supplies bounded line ranges from matching local SDK sources,
the `decimal` kernel topic, and study/strategy/indicator templates. `template=basic` renders declared
subscriptions into the matching template. Version and ABI metadata come from
the SDK's `Cargo.toml` and `src/abi.rs` and must match the canonical packaged SDK.
SDK identity hashes Cargo manifests and Rust source files across `strategy-sdk`,
`contracts`, and `num`; dependency changes invalidate stored validation. The wheel
and source distribution carry these canonical crates with portable relative Cargo
dependencies, not a separate Python-side SDK copy. Remote harness access does not
supply local references. Missing or mismatched sources fail explicitly. The
study/strategy/indicator templates in `programs.py` are shared by reference reads
and basic authoring; they are minimal SDK contracts, not completed research models.

Model creation and strategy design complete with artifact references
(`workspaceId`, `kind`, `programId`, `gitCommit`, content-hash `version`), not inline
Rust. Model references also carry research description, outputs, and tests.
`session_services.py` verifies the pinned reachable commit against the latest
content and accepted stored validation, including file hashes and SDK identity;
the reference commit must match the latest validation's source commit. Completion
does not compile, run, or rewrite source. Workflow composition uses the
`workflows/research_models.py::ProgramSnapshots` read port to resolve accepted
commits and their dependency snapshots; later edits cannot silently change the
accepted model. Prompts own SDK lookup, targeted repair, and conversational review;
there is no runtime compiler-repair cap. The general session iteration ceiling
and cancellation remain separate safety boundaries.

### Workflow Composition

`spec.phase_declarations()` is the one authoritative declaration of live phases,
edges/pipes, schemas, executor/tool/knowledge policies, and payload folds.
Secondary lookup maps and the registered definition derive from it. Live
acceptance and durable replay both apply the pure, I/O-free
`rebuild.fold_transition` through the core's `reduce_transition` port: validate
source/sequence/edge, fold completion payload, then apply the declared pipe once.
Rebuild reads transitions, not the workspace hypothesis side-store.

Acceptance verifies current intent epoch and the owning `phase_executor`,
execution/chat/parent/phase, and attempt identity. Under the shared
`ChatService.execution_guard` used by steering, cancellation, and job attachment,
it rechecks those facts, commits the transition with execution-store revision CAS,
projects the phase, and closes the accepted executor before releasing the guard.
Events, child-result delivery, and successor entry occur outside that guard.
This is a single-process fence plus store CAS, not a cross-store transaction.

The HTTP factory registers root and `/api` routes on the same app; both resolve
the same app-state services and authentication. Defaults are fresh per factory,
composed once, and never created lazily by requests. Production explicitly uses
`in_memory=False`; absent services return typed 503 errors. `http/schema.py`
exports the app's OpenAPI 3.1 plus runtime activity/message/datum/tool schemas;
`http/wire.py` derives durable record fields from their dataclasses and response
envelopes from typed models, including complete input receipts and tree records.
Schema generation and wire validation are implemented. This does not establish
complete side-effect recovery or output-publication receipts for every operation.

Cosmos commits a message or event by writing the per-chat sequence counter CAS
and the item create in one transactional batch in the workspace partition.
Messages, events, and immutable first-save execution snapshots are the same
documents the timeline reads, so there is no catalog-to-timeline projection,
deferred repair, or second container. A failed batch consumes no sequence;
concurrent writers retry conflicts. Timeline readers snapshot the in-process
commit version before reading durable items and wait for a version change;
the database sequence remains the only cursor, and a recovery interval bounds a
lost wake. `open_cosmos_chat_stack` and `open_execution_store` call
`ensure_database`, which creates the database and catalog/executions containers
if absent. Opening these stacks is not side-effect-free.

The harness `run_study` tool is implemented across the Rust workspace and the
harness-hosted MCP endpoint:

- `run_study` is a capability-gated MCP tool with a `StudySubmitter` boundary,
  a remote `POST /v1/studies/jobs` route, and a `harness study-job` entrypoint
  that writes a `study.json` artifact with the `study` shell;
- the study execution mode is Rust-JIT: the authored source compiles through
  `harness-strategy-jit` against `harness-strategy-sdk` ABI v4 and implements
  `MarketConsumer<Context = ObservationContext>` plus `StudyProgram`
  (`new(parameters)`, `on_stop(&mut MetricPublisher)`). It observes subscribed
  quotes, trades, and atomic frames through the common callbacks, has no order
  path by construction, never submits an order, and is never
  recorded as a backtest;
- `validate_study` is the capability-gated authoring gate beside `run_study`:
  it takes the authored model document itself (`model.source` plus its
  `harnessConfiguration.start`/`.end`), derives the runtime study job
  internally, and compiles, loads, and constructs the program over the managed
  JIT workspace without replaying data. The agent-facing workspace wrapper takes
  only `program_id` and loads committed source/configuration before calling that
  harness boundary. The model-creation agent calls it before submitting completion;
  completion verifies the stored receipt without recompiling. A source that
  cannot build is repaired in the asking agent's turn instead of failing the
  trials phase after acceptance;
- a research model is an authored Rust study program published as a workspace
  artifact whose `on_stop` publishes its fitted parameters as `fit.<parameter>`
  metrics; Thesis freezes them as the content-addressed fitted-model artifact;
- Thesis now calls `run_study` through `McpStudyRunner` over the harness-hosted
  MCP endpoint, and its research-model bundle declares the `study` shell, the
  exact declared subscriptions, and the authored Rust study program.

The `strategy_design` phase is conversational and prose-first
(`prompts/strategy_design.py`): it posts the strategy proposal under
`### Strategy`, `### Entries & Exits`, and `### Assumption Monitors` — separating
trading exits from monitors that re-check the entry's premises — asks
through `request_workflow_input`, and finalizes only after the operator
confirms. Strategies are pure Rust authored against `harness-strategy-sdk`
and are committed workspace programs under `strategies/<program_id>/`, never a
standalone store record. The program-bound `author_strategy`/`update_strategy`
tools commit source; `validate_strategy` compiles through the harness before
reference-based completion. The older `WorkspaceStrategyArtifacts` service and
Strategies HTTP projection remain separate domain surfaces, not the program-bound
workflow's authoring or completion path.

The implemented boundaries above do not establish completion of every normative
repair-budget or restart requirement below. Operator workflow gates remain
removed; domain contract/evidence validation is distinct from conversational
operator review.

### Accepted v1.0.1 decisions

Conversation anchoring and datum rendering were settled in review and are
normative for the migration; see "Chat Surface, Anchoring, and Datum Rendering"
below and the accepted design record
`thesis-chat-surface-and-subagent-anchoring-v1`. In brief: a sub-agent is the same
conversation abstraction as the top-level agent, nested; the caller's anchor
decides where a datum renders; tools return typed datums (possibly several, of
different types, asynchronously) and the shared surface maps datum type to
rendering; workflow executions are owned by the anchor that launched them; a
durable workflow-node record owns the phase agents; an execution-scoped sub-agent
tree is replaced by durable-record trees; and node input is governed by a
per-node operator policy whose default is human-only.

## Overview

Thesis workflows remain deterministic state machines over named phases. An
agent-backed phase is implemented on top of a durable, chat-attached sub-agent;
the workflow does not own the sub-agent's conversation or provenance. A phase
agent calls permitted Thesis and harness tools directly and requests phase
completion by calling `request_phase_completion` with a typed completion
contract. Deterministic workflow code validates the submitted contract and
recorded evidence, then selects the legal transition.

Every sub-agent, turn, model invocation, tool invocation, tool result, phase
attempt, completion request, validation result, and transition is durable. A
process restart reconstructs the same agent conversation and workflow state
without inventing or dropping intermediate activity.

## Goal

- Preserve a deterministic, auditable phase graph.
- Make agent-backed phases genuinely agent-driven rather than model-authored
  content inside code-driven tool execution.
- Let every agent-backed phase call its permitted MCP tools directly.
- Make sub-agents independently durable and usable from either chat or a
  workflow phase.
- Preserve the current model-creation and verdict user experience.
- Validate phase completion contracts and evidence deterministically.
- Split the runtime and workflows into maintainable, nested packages.

## Non-Goals

- Letting a model name or bypass arbitrary workflow transitions.
- Trusting model-authored run identifiers, metrics, hashes, or artifacts
  without resolving them against durable records.
- Allowing every sub-agent to call every tool.
- Mutating an in-flight provider request when operator input arrives.
- Making quant-harness responsible for Thesis workflow state.

## Out-of-Scope

This specification does not select model providers, define the visual design of
the Thesis UI, set broker or activation policy, or prescribe quant-harness
internals beyond the published `run_study` MCP contract and trusted
attribution requirements.

## Definitions

| Term | Definition |
| --- | --- |
| Chat | The durable top-level conversation and ownership boundary for sub-agents. |
| Sub-agent | An independently durable agent attached directly to a chat. It owns a conversation, specification snapshot, tool policy, invocation history, and result. |
| Agent session | The runtime that resumes a top-level chat agent or sub-agent, invokes a model, dispatches tool calls, persists turns and provenance, and applies budgets. |
| Agent turn | One scheduled run of an agent session, triggered by launch, operator input, repair, or resume. It may contain multiple model invocations and tool calls. |
| Workflow execution | A durable instance of a versioned deterministic phase state machine. |
| Phase attempt | One entry into a workflow phase. An agent-backed attempt references a durable sub-agent; a deterministic attempt references a deterministic run record. |
| Conversation phase | An agent-backed phase that accepts unsolicited operator messages while running. |
| One-shot phase | An agent-backed phase that does not accept unsolicited operator messages while running. It may still require multiple agent turns for repair or requested operator input. |
| Deterministic phase | A phase implemented only by deterministic code, with no model or sub-agent. |
| Completion contract | The phase-specific JSON value supplied to `request_phase_completion`. |
| Construction | Parsing, validating, and normalizing a completion contract into a typed domain object. |
| Acceptance gate | Deterministic code that validates a constructed contract against durable workflow state and evidence. |
| Repair turn | Durable incoming runtime feedback that explains why no completion request was accepted and asks the same sub-agent to try again. |
| Study | A durable market replay execution with no order execution. It runs one authored Rust `StudyProgram` over exactly its subscribed lake inputs (bars, quotes, and/or trades, including owned indicator requirements); a research-model program publishes fitted parameters as `fit.<parameter>` metrics in `on_stop`. It is never represented as a backtest or a strategy. |
| Tool context | Trusted attribution attached by the runtime to a tool invocation. Models cannot provide or override it. |

## Requirements

**REQ-DEL: Neutral delivery and executable enforcement.** Roots, helpers, phases,
and model-only sessions MUST follow the Option 1 contract above, with one neutral
inbox, one incoming storage writer, one pure live/replay provider projection,
opaque request audits, success-bound ACKs, and no alternate execution path.
The coordinator owns test edits and integration verification after edits freeze;
this documentation-only update does not certify implementation or test results.

Required assertions and verification paths:

| Assertion | Verification path |
| --- | --- |
| Required modules fail when absent; AST imports/calls and aliases reject native session construction, hidden incoming writers, parallel result queues, and adapter effects; reference-only audits, unconditional pre-provider persistence, terminal preservation, exact root reference/globs, and mutation controls remain mandatory with no optional bypass | `tests/thesis/agent/runtime/test_delivery_architecture.py` |
| Every source (operator, initial prompt, tool result, helper result, workflow outcome, runtime notification/check, repair, asynchronous completion) converges on the writer; receipt identity, arrival order, assigned input position, retries, visibility-before-wake prevention, and one receipt for multi-datum outcomes | `tests/thesis/agent/runtime/test_canonical_delivery.py`, `test_initial_intake.py`, `test_session_delivery.py`, `test_session_live_activity.py` in the same directory |
| Canonical decode and live/replay equivalence, exact scoped correlation, only runtime-configured initial bootstrap projects as native system, a record without canonical session data is not decoded, foreign/reused IDs, partial groups, and interleaved inputs without fabrication or privilege | `tests/thesis/providers/test_session_adapter.py`, `tests/thesis/agent/runtime/test_session_delivery.py` |
| Mandatory opaque audit retains actual protocol mapping, advertised tool catalog and their versions/options, ordered canonical message references and request IDs without full canonical duplication; detached, hashed caller persistence and Cosmos request chunks precede execution and survive success/failure terminal updates; adapter stays pure and audit/output persistence faults prevent execution/ACK/dispatch as applicable | `tests/thesis/agent/runtime/test_session_input_snapshot.py`, `tests/thesis/providers/test_session_adapter.py`, `tests/thesis/cosmos/test_chunks.py` |
| Live inbox reconciles scoped authoritative durable order at each eligible model boundary; arrivals after provider pull and before output persistence never rearrange the live snapshot | `tests/thesis/agent/runtime/test_arrival_reconciliation.py`, `tests/thesis/agent/runtime/test_session_input_snapshot.py` |
| Only pending inputs actually included in a successful provider request are ACKed after output persistence; empty/unusable completion interrupts without ACK or invented empty reply; initial pending input, mid-model and post-provider arrivals, interrupted/provider/output/ACK faults, partial activity, and results after terminal requests stay truthful and unconsumed until eligible | `tests/thesis/agent/runtime/test_session_delivery.py`, `test_canonical_delivery.py`, `test_session_input_snapshot.py` in the same directory; `tests/thesis/agent/runtime/test_arrival_reconciliation.py` |
| Synchronous and asynchronous completion, skipped/not-started calls, unknown mutation outcomes, terminal/cancelled late delivery, and private child transcripts do not resurrect nodes or replay effects | `tests/thesis/agent/runtime/test_canonical_delivery.py`, `test_session_live_activity.py`, `test_durable_architecture.py` in the same directory; `tests/thesis/workflows/registry/hypothesis_development/test_live_tool_recovery.py` |
| Restart/reload/resume preserve all incoming sources and per-anchor input/consumed positions, pending partial groups, correlations, idempotency, delayed-wake fences, and the same projection, without synthetic results or side-effect replay | `tests/thesis/agent/runtime/test_durable_architecture.py`, `test_turn_retirement.py`, `test_unified_conversation.py` in the same directory |
| Steering epoch is installed before canonical wake for synchronous and asynchronous workers; root/model-only routing, source completion, node lifecycle, store CAS, and effects outside the acceptance guard remain unchanged; deterministic executor faults still fail workflows | `tests/thesis/workflows/test_steering_delivery.py`, `test_unified_session.py` in the same directory; `tests/thesis/http/test_input_acknowledgement_wiring.py`, `test_serving_midturn.py` in the same directory; `tests/thesis/workflows/registry/hypothesis_development/test_acceptance_fence.py`; `tests/thesis/agent/runtime/test_lifecycle_acceptance_guard.py` |

These suites are required verification locations, not a claim that every row is
already completely asserted. The coordinator MUST close/report coverage gaps,
including all-source restart/input-position and pending-partial cases, and run
the canonical focused `uv run --extra dev pytest -p no:cacheprovider <paths>`
checks. Repository-wide verification remains the coordinator's one post-freeze
integration pass under the root execution policy.

**REQ-001: Deterministic phase machine.** A workflow MUST define an explicit,
versioned set of phases and legal transitions. Only deterministic workflow code
MAY persist a `PhaseTransition`.

- Rationale: Model output can propose an outcome but cannot be the authority
  for workflow legality.
- Verification: Attempting an undeclared transition fails without changing the
  execution phase.

**REQ-002: Explicit phase kinds.** Every phase MUST be declared as exactly one of
`conversation`, `one_shot`, `deterministic`, or `terminal`. Terminal outcomes have
neither an executor nor a completion contract.

- Verification: Workflow registration rejects a missing or unsupported phase
  kind.

**REQ-003: Sub-agent foundation.** Every conversation or one-shot phase MUST
run through a durable sub-agent attached directly to the execution's chat. The
phase attempt MUST reference the sub-agent id. A deterministic phase MUST NOT
create a sub-agent solely to represent its execution. Terminal phases MUST NOT
create an executor or sub-agent.

- Rationale: Sub-agents are a general chat capability. Workflow phases are one
  consumer of that capability, not its owner.
- Verification: Deleting or rebuilding a workflow projection does not delete
  the referenced sub-agent conversation.

**REQ-004: Direct tool access.** Models in conversation and one-shot phases
MUST receive the permitted MCP and Thesis tool catalog and MUST be able to call
those tools through the normal model tool-calling protocol. Workflow code MUST
NOT impersonate a model tool call or synthesize a model-authored tool event.

**REQ-005: Agent-requested completion.** An agent-backed phase MUST remain
active until its owning phase sub-agent successfully calls
`request_phase_completion`, or until the execution is paused, stopped,
cancelled, or failed. Producing prose alone MUST NOT complete an agent-backed
phase.

**REQ-006: Deterministic acceptance.** A valid completion request MUST be
evaluated by the phase's deterministic acceptance gate. The model supplies the
contract and evidence references; the gate determines `advance`, `loopback`,
`remain`, or `fail` and selects any target through the declared transition
table.

**REQ-007: Declared targets only.** A completion contract MAY name a target
from the phase's declared successors; the engine MUST validate it against the
registered transition table. A phase with `on_complete` MAY omit the target and
use that declared successor. Model output MUST NOT introduce an arbitrary edge.

**REQ-008: Complete durability.** All sub-agent state, conversation turns,
model invocations, tool invocations, results, completion requests, validation
results, and workflow transitions MUST be persisted before their effects are
treated as visible or complete.

**REQ-009: Caller receives tool result.** Every tool invocation MUST deliver one
durable neutral incoming receipt to the conversation of the agent that invoked it. Tool
results MUST NOT be delivered only through an execution event, UI stream, or
child conversation.

**REQ-010: Per-sub-agent tool policy.** Every sub-agent MUST have an immutable
allowed-tool policy snapshot for its current specification version. Tool
dispatch MUST be default-deny and MUST validate the requested tool against the
calling sub-agent's effective policy.

## Runtime Model

### Durable Sub-Agent

A sub-agent is attached to a chat, not to a workflow execution. Its durable
record MUST include at least:

```text
SubAgentRecord
  sub_agent_id
  chat_id
  workspace_id
  parent_sub_agent_id?          # null for a chat-level child
  spawned_by_invocation_id
  workflow_execution_id?       # attribution only
  phase_attempt_id?             # attribution only
  spec_version
  kind                         # conversation | one_shot (input policy)
  completion_mode              # complete_on_text | explicit_completion
  returns_to_sub_agent_id?      # null for the root return anchor
  phase_executor               # explicit workflow-completion authority
  role
  allowed_tools
  status                       # pending | running | needs_input | completed | failed | cancelled
  accepts_input
  input_contract_ref?
  completion_contract_ref?
  result_ref?
  created_at
  updated_at
```

`workflow_execution_id` and `phase_attempt_id` are optional attribution links.
They do not make the workflow the owner of the sub-agent.

Each sub-agent owns a durable ordered conversation. Chat records retain
`system`, `user`, `assistant`, and `tool` presentation fields, while canonical
content is persisted under `data['sessionMessage']`. Decoding is canonical-only;
neither presentation nor native audit roles are session message types. Provider projection MUST preserve explicit call/result correlation
without rewriting durable arrival order. A turn MAY link to first-class
invocation records.

### First-Class Invocations

Model and tool calls MUST be first-class durable invocation records, not facts
inferred from UI events. A unified invocation family SHOULD use a discriminator
such as `model` or `tool` and MUST retain:

- invocation id and idempotency key;
- workspace, chat, and conversation ids, plus an optional sub-agent id;
- optional workflow execution and phase attempt ids;
- parent invocation id for a tool call requested by a model invocation;
- role policy, model profile, skill, and tool-policy hashes;
- input or argument hash and referenced artifacts;
- output or result hash and referenced artifacts;
- requested, started, terminal, and failure state;
- timestamps and credential-free failure details.

Execution events MAY reference these invocation ids for streaming and
projection, but events MUST NOT be the sole provenance store.

### Agent Session

One `AgentSession` abstraction MUST serve top-level chat replies, chat-spawned
sub-agents, and workflow phase sub-agents. It MUST:

1. Load the durable root-chat or sub-agent conversation owner and its ordered
   conversation.
2. Resolve the role, model profile, skills, and allowed tools.
3. Persist a model invocation request before contacting the provider.
4. Persist the assistant response or tool-call turn before dispatching tools.
5. Dispatch each tool with trusted tool context.
6. Persist each tool invocation before dispatch and deliver its result through
   the injected inbox persistence port before subsequent provider projection.
7. Repeat until the model yields, a control tool is accepted, a process fault
   occurs, or a safety budget stops the turn.
8. Save a checkpoint after each durable boundary so recovery never repeats a
   completed side effect.

Root chat and phase/helper children use this abstraction through the conversation
service, including model-only composition with an empty registry. There is no
separate plain reply fallback or legacy executor.

## Operator Input Semantics

**REQ-011: Conversation input.** A conversation phase MUST accept operator
messages while the sub-agent is running. An incoming message MUST be persisted
immediately but MUST NOT mutate an in-flight provider request. The session MUST
consume it at the next model-invocation boundary.

**REQ-012: One-shot input.** A one-shot phase MUST reject unsolicited operator
messages while running. It MUST accept an operator message after its sub-agent
has called `request_operator_input` and entered `needs_input`.

**REQ-013: Agent-authored request.** `request_operator_input` MUST accept the
operator-facing message and a structured reason from the calling agent. On
acceptance, it MUST append the message durably, emit `SUB_AGENT_NEEDS_INPUT`,
set `accepts_input=true`, return an acknowledgement to the caller, and end the
current agent turn.

Any agent, including a one-shot phase agent or dynamically spawned child, MAY
call `request_operator_input` if its tool policy permits the control tool.

The distinction between conversation and one-shot is therefore only whether
the operator can send unsolicited messages during execution. It does not mean
that a one-shot phase is guaranteed to use one model invocation.

## Tool Architecture

### Unified Catalog

An agent session MUST compose one catalog from:

- quant-harness MCP tools;
- workspace and artifact tools;
- Thesis control tools; and
- any domain tools allowed by the sub-agent specification.

The dispatcher MUST intersect the catalog with the sub-agent's immutable
allowed-tool policy. A tool absent from the intersection MUST be rejected
before transport dispatch.

### Trusted Tool Context

Every dispatched tool MUST receive trusted context attached by the Thesis
runtime, outside model-controlled arguments:

```text
ToolContext
  workspace_id
  chat_id
  conversation_id
  sub_agent_id?
  workflow_execution_id?
  phase_attempt_id?
  model_invocation_id
  tool_invocation_id
  idempotency_key
  actor_id
```

The model MUST NOT be able to override these values. Harness tools MUST record
the supplied context with the resulting job, run, fit, sweep, or artifact.

### Thesis Control Tools

The initial control catalog MUST include:

| Tool | Purpose |
| --- | --- |
| `request_phase_completion` | Submit the owning workflow phase's typed completion contract. Available only to the phase's root sub-agent. |
| `request_sub_agent_completion` | Submit a non-phase sub-agent's specification-specific terminal result. |
| `request_operator_input` | Publish an agent-authored question and pause for an operator reply. |
| `spawn_sub_agent` | Create a durable chat-attached child using an allowed specification. |
| `await_sub_agent` | Return a child's current state or terminal result to the calling agent. |

All sub-agent creation MUST pass through the `spawn_sub_agent` command handler.
A workflow deterministically starting a phase MUST invoke that same command
contract as a first-class tool invocation with workflow actor scope and MUST
persist its returned sub-agent handle.
A model dynamically creating a helper invokes it as a normal tool call.

`spawn_sub_agent` is asynchronous. Its immediate result MUST return the durable
child id and initial state to the caller. `await_sub_agent` MUST return either a
terminal result or a non-terminal status such as `running` or `needs_input`.
Child output MUST NOT be injected into the parent conversation implicitly.
The child requests terminal completion through `request_sub_agent_completion`;
the result is constructed against the completion contract captured in its
specification. The parent obtains that result through `await_sub_agent`.

Each child MUST inherit workspace and chat scope. A child MUST receive a budget
no greater than its parent's remaining delegated budget. Spawn authorization,
maximum depth, and maximum active children MUST be enforced deterministically.
Spawned sub-agents MUST be `conversation` or `one_shot`; deterministic work is
not represented as a sub-agent.

`request_sub_agent_completion` MUST use the same typed construction feedback
and repair-turn machinery as phase completion. Its deterministic lifecycle
validator replaces the workflow acceptance gate. Acceptance marks the child
completed and freezes its terminal result for `await_sub_agent`.

## Phase Completion Protocol

### Request Contract

`request_phase_completion` has a stable tool name but a phase-specific input
schema. The tool schema exposed to the model MUST wrap the registered completion
contract:

```json
{
  "completion": {}
}
```

The model does not supply execution, phase, target, or sub-agent identifiers.
The runtime resolves them from trusted session context.

### Construction and Acceptance

The tool handler MUST process a completion request in this order:

1. Decode the tool arguments.
2. Construct the phase-specific typed completion contract.
3. On construction failure, persist the failed request and return a structured
   tool result containing field-level errors and the required schema.
4. On construction success, persist the normalized contract.
5. Evaluate the deterministic acceptance gate against durable state and
   evidence.
6. Persist the gate result.
7. Return the acceptance result to the calling agent as a durable incoming receipt.
8. If the result is `advance`, `loopback`, or `fail`, apply the corresponding
   deterministic state-machine action after the tool result is durable.

An `advance`, `loopback`, or `fail` result MUST end the current phase-agent
turn. A `remain` result keeps the turn active and MAY be followed by evidence
gathering, another completion request, or an operator-input request.

Acceptance commits the transition and then enters the successor phase through
the runtime's detach seam. The completion tool call MUST return once its
transition is committed; it MUST NOT stay open while the successor phase runs.
An agent successor is spawned and woken through the conversation service, which
already schedules turns out of band; a deterministic successor (for example the
falsification trials) runs as its own detached unit of work, records its own
phase result and transition, and a fault there fails the execution durably
instead of surfacing inside the completed phase's tool call. A composition
without a detach seam (tests, embedders) enters the successor inline.

An acceptance failure caused by missing or contradictory evidence is not a
schema construction failure. It MUST return `remain` with violations and leave
the phase active so the agent can call more tools, revise its evidence, request
operator input, or submit again.

### Nested Repair Budgets

The following is the accepted design baseline, not current enforcement. The live
completion path returns in-turn validation feedback but has no runtime repair-turn
counter or compiler-repair cap. `SubAgentBudget` fields alone do not enforce these
limits; `prompts/sdk.py` asks for operator input after three unsuccessful compiler
repairs. The session retains its independent tool-iteration safety ceiling.

Each agent turn permits three failed completion-contract construction attempts.
The result of each attempt is returned to the model in the same agent turn. An
accepted request ends the repair sequence.

If an agent turn ends without an accepted request:

- a conversation phase that has produced ordinary agent prose yields to the
  operator and enters `needs_input`;
- a one-shot phase receives a durable repair turn; and
- either phase receives a durable repair turn if its construction-attempt
  budget was exhausted.

Before a conversation phase enters `needs_input`, the session MUST check for an
unconsumed operator message that arrived while the turn was running. If one
exists, the session schedules the next agent turn with that message instead of
emitting a redundant input request.

Each phase permits two repair turns after its initial turn. Each repair turn
again permits three construction attempts. Therefore an agent can make at most
nine failed construction submissions across the initial turn and two repair
turns before mandatory operator escalation.

After two unsuccessful repair turns, the runtime MUST enter `needs_input` and
present the accumulated construction or acceptance shortfall to the operator.
Before pausing, the runtime MUST give the agent one control-only escalation
invocation in which it can author the operator request through
`request_operator_input`. If model generation is unavailable, the raw durable
shortfall is the fallback; deterministic code MUST NOT invent conversational
wording.
The operator response resumes the same durable sub-agent and phase attempt;
it starts a new, explicitly numbered repair epoch with a fresh initial turn and
two repair turns. Repair-epoch counters and budgets MUST be durable.

The runtime SHOULD configure high execution and ordinary tool-iteration
budgets during the initial implementation, but MUST retain a hard administrative
ceiling, cancellation path, and idempotent recovery. It MUST NOT use an
unbounded loop.

## Deterministic Phase Semantics

A deterministic phase executes no model invocation and owns no phase
sub-agent. Its implementation returns a typed outcome:

```text
DeterministicPhaseOutcome
  action: advance | loopback | fail | pause
  reason
  artifacts[]
  evidence_refs[]
```

The workflow runner validates the action against the transition table, persists
the result, and applies it. Deterministic phase failures MUST retain the partial
result and failure detail.

A deterministic phase run is dispatched off the phase-completion call: the
completed phase's agent tool call returns when its transition commits, and the
deterministic run executes as its own unit, persisting its phase result and its
own transition before the next phase begins. Its failure is the execution's
failure, recorded and delivered durably — never a silently wedged execution and
never a delayed tool response.

A deterministic executor or its invalid completion contract MUST fail the
workflow. It MUST NOT be converted into an agent-recoverable tool failure or
spawn an agent to repair deterministic execution. This is distinct from an
agent's tool error or an interrupted model turn.

`McpBacktestRunner` and `McpStudyRunner` send configuration mappings inline;
explicit path strings refer to the harness host. They join only confirmed
in-flight jobs and return a terminal job's stored result instead of relaunching
it under the same `job_id`; an explicit rerun allocates a fresh job. A submitter
allocates one `job_id` and durably registers the submission with immutable
program/model attribution before dispatch, so an interruption after harness
acceptance is reconciled by that retained reference even before trial or report
evidence exists, and a stale revision's reference is never reused. The exact
domain not-found response permits a launch; an unavailable transport,
authentication/protocol error, or unknown state is not evidence of absence and
fails the deterministic call. Polling has a monotonic deadline, bounded backoff,
and cancellation checks before launch and around status queries. See the
"Execution Job Contract" in `.github/instructions/copilot-instructions.md`.

Workflow failure outcomes are delivered to the launching anchor and wake that
conversation so its agent can surface the failure to the operator. Whether a
new execution starts is the agent's judgement from the conversation — a
failure notification alone is not authorization. Root-chat history retains
tool-result evidence so the outcome is visible: results with their assistant
call envelopes replay as native tool messages, while unattached results and
asynchronous notifications replay as structured inbound notes.

Before model creation completes, the newly submitted falsifier
observables must be declared in that submission's `model.outputs`, and its
source must contain exact literal publications for those outputs. A mismatch
returns a repair response before the phase transition commits. Prior model
outputs do not satisfy a revised model's contract. The trials phase retains
its metric-presence check for publications skipped by runtime branches.

Model-creation argument repair preserves the operator-approved statistical model
and complete falsifier suite; diagnostic stubs are not research completions.
The prompt and completion schema explicitly distinguish testing data
availability from testing the claimed regularity and its negative control.
In verdict, an explicit operator instruction to loop back authorizes that
target without asking for the same direction a second time. Running
conversation nodes retain input access after consuming an answer; one-shot
nodes do not. Terminal nodes are not run again by a queued worker turn.

## Harness Study Tool

Quant-harness MUST publish `run_study` as a real MCP tool, served by the
harness-hosted MCP endpoint. A **study** is a durable market replay execution with
no order execution. The tool replaces the synthetic `harness.research_model`
label and Thesis-side direct invocation of `ResearchModelRunner` from workflow
code.

### A Study Is Not A Backtest

A study is a statistical replay, not a trading strategy. When it runs a
research model, that model is an authored Rust program compiled through
`harness-strategy-jit` against `harness-strategy-sdk` ABI v4. It implements
`MarketConsumer<Context = ObservationContext>` for market callbacks and
`StudyProgram` for construction (`new(parameters)`) and final publication
(`on_stop(&mut MetricPublisher)`). Fitted parameters use `fit.<parameter>`;
observables use their exact declared output names. `ObservationContext` has no
order capability, and a study sees no position, sizing, portfolio, or venue
surface. Strategies implement `MarketConsumer<Context = OrderSink>` for the
same quote/trade/frame callbacks; `StrategyProgram` retains `new`, `on_start`,
`on_fill`, and `on_stop`, with execution, costs, and risk owned by the harness.

`subscriptions(&self) -> Vec<Subscription>` declares each consumer's own inputs:
`Bars { symbols, interval }` (`1min` or `1d`), `Quote { symbols }`, or
`Trade { symbols }`. `indicators(&mut self) -> Vec<&mut dyn Indicator>` exposes
references to owned dependencies, including nested indicators. SDK indicators
implement `MarketConsumer<Context = ObservationContext>` plus `Indicator::value`.
Declarations and topology MUST remain stable after construction; cycles and
zero-sized indicator dependencies are rejected. Dependencies update before
their parent, and routing filters every consumer's own types, symbols, and
interval. Authored bar callbacks use `on_bar(&BarFrame, &mut Self::Context)`;
there is no raw single-bar study callback. The ABI's internal `on_bar_frame`
vtable slot is not an authored trait method.

Running a study through the backtest boundary is prohibited because it conflates
two different contracts and produces a "backtest run" record, manifest, artifact
kind, and tool name for what is a statistical replay. That corrupts the
experiment ledger, the evidence-grade taxonomy, and the operator-visible report.

The deterministic point-in-time replay substrate (data ensure, point-in-time
resolution, session calendars, corporate actions, artifact publication, and run
persistence) MAY be reused. Reuse MUST be through a distinct study execution
mode that binds a no-order study program and a distinct study run kind. A
research model MUST NOT be represented as a `Strategy`, and
`ResearchModelAdapter(Strategy)` stays removed. Studies MUST have their own
study record, manifest, and artifact kind so the ledger, evidence grade, and UI
label them as studies.

The phase acceptance gate MUST bind falsifiers to the study's recorded metric
references, never to a backtest run.

### Execution Path

The tool spans the harness Rust workspace and the harness-hosted MCP endpoint:

1. `run_study` (`TOOL_RUN_STUDY`) is a capability-gated MCP tool with a
   `StudySubmitter` boundary. The local submitter compiles and runs the
   authored study configuration through
   `harness-strategy-jit::run_study_configuration`; the remote boundary posts
   the configuration to `POST /v1/studies/jobs`; and the `harness study-job`
   entrypoint runs one configuration and writes the canonical `study.json`.
2. The study execution mode is Rust-JIT: the authored source compiles against
   the pinned SDK ABI (the loader rejects an artifact built against a different
   version) and is driven over exact subscribed inputs, never binding order,
   position, sizing, portfolio, or venue capability.
3. The study configuration declares `shell: study` and `data.subscriptions`.
   After construction, ABI v4 exposes the canonical source-and-dependency union
   as JSON. The echo MUST canonically equal that union before acquisition.
   The harness ensures and reads exactly those types, symbols, and bar intervals
   through the market lake via
   `harness-lakeclient`; the harness never reads lake files or links lake
   storage, and the lake can never be bypassed with a local path.
4. Thesis's research-model adapter preserves
   `harnessConfiguration.subscriptions` as `data.subscriptions` and materializes
   the declared window without widening it. `validate_study` compiles, loads,
   constructs, returns effective `subscriptions`, and rejects an unequal echo
   without fetching data. Only one distinct bars interval is supported across
   a run, including dependencies and instances; conflicts fail before any lake
   ensure/read. Tick-only studies need no interval. Interval-only documents are
   rejected; authored source MUST use the current SDK API and compiled artifacts
   MUST rebuild for ABI v4.
5. Thesis calls `run_study` through `McpStudyRunner` over the hosted MCP
   endpoint and binds phase completion to the returned study's metric
   references; the `fit.<parameter>` metrics freeze as the content-addressed
   fitted-model artifact.

### Input

An existing study configuration document is passed by path (`config_path`) or
inline (`config`), exactly like `run_backtest`. The configuration declares the
`study` shell, the sample window, and the `data` block with exact subscriptions,
and one JIT-authored program instance whose
`parameters.source` is the Rust study program and whose `bundle_hash` equals
the SHA-256 of that source:

```yaml
shell: study
job_id: job-...
start: "2020-01-01T00:00:00Z"
end: "2025-12-31T00:00:00Z"
data:
  symbols: [SPY]
  subscriptions:
    - data_type: bars
      symbols: [SPY]
      interval: 1d
strategy_instances:
  - instance_id: research-model-...
    implementation: jit
    bundle_hash: "<sha256 of the authored source>"
    capital_fraction: "1.0"
    symbols: [SPY]
    parameters:
      source: |
        use harness_strategy_sdk::serde_json::Value;
        use harness_strategy_sdk::{
            export_study_program, BarFrame, MarketConsumer, MetricPublisher,
            ObservationContext, StudyProgram, Subscription,
        };

        struct Model;

        impl MarketConsumer for Model {
            type Context = ObservationContext;

            fn subscriptions(&self) -> Vec<Subscription> {
                vec![Subscription::Bars {
                    symbols: vec!["SPY".into()],
                    interval: "1d".into(),
                }]
            }

            fn on_bar(&mut self, _frame: &BarFrame, _ctx: &mut ObservationContext) {}
        }

        impl StudyProgram for Model {
            fn new(_parameters: Value) -> Self {
                Self
            }

            fn on_stop(&mut self, metrics: &mut MetricPublisher) {
                let _ = metrics.publish("fit.baseline", 0.5);
            }
        }

        export_study_program!(Model);
```

Trusted tool context supplies attribution and idempotency. Those fields MUST
NOT appear in model-controlled arguments.

### Execution

The tool MUST:

1. Compile the configuration's authored Rust program through the JIT seam and
   load it behind the SDK ABI (cached by source digest, program kind, SDK
   version, toolchain digest, and SDK ABI version).
2. Resolve and validate the compiled source-and-dependency union and optional
   config echo before acquisition. Ensure/read exactly the study's effective
   subscriptions through lake routes; no direct lake-file access exists. Bars
   ensures fetch provider bars or derive from already stored trades, never
   trigger undeclared trade fetching. Backtests separately add quote/trade
   execution feeds. The manifest distinguishes `effective_subscriptions`,
   `execution_subscriptions`, and their deduplicated `acquired_subscriptions`
   union; execution inputs do not belong in the consumer echo.
3. Assemble subscribed same-interval bars into atomic frames and call common
   `MarketConsumer` callbacks with dependency-first, consumer-local routing.
   Mixed replay uses one availability-ordered causal queue: at equal time,
   ticks precede completed frames, then generated execution events. Call
   `on_bar` at frame availability, never bar start, and `on_stop` once after
   replay.
4. Collect the metrics published by `on_stop`: every publication is validated
   host-side (finite value, non-reserved name), a rejected publication fails
   the run even when the program ignores it, and the model's fitted parameters
   are the `fit.<parameter>` metrics.
5. Freeze immutable fitted parameters and calibration bounds as the
   content-addressed fitted-model artifact.
6. Publish the canonical `study.json` run artifact and its result set
    (including `metrics.json`) under the study artifact kind.

Each `BarFrame` carries `interval`, `interval_start_ns`, `interval_end_ns`,
`available_at_ns`, exact-decimal `bars_by_symbol`, and explicit
`missing_symbols`. The harness uses the observed interval union: it does not
invent all-missing frames, forward-fill, or join symbols by row position.
Availability equals interval end under a historical end-of-interval assumption,
not measured provider latency or revision availability. Frames beyond the run
horizon are withheld; availability exactly at the horizon is included. Equity
daily bounds use real XNYS sessions including DST and early closes; crypto
daily bounds use UTC days. Mixed equity/crypto daily intervals are rejected.
Duplicate symbol/interval rows, unexpected symbols, and off-grid starts fail
validation. Raw replay counts/fingerprints remain distinct from frame dispatch
counts. SDK ABI v4 invalidates older compiled artifacts; authored studies and
strategies MUST move market methods into `MarketConsumer` and declare exact
subscriptions. `new` and `on_stop` remain in the specialized program impl;
strategies also retain `on_start` and `on_fill` there.
7. Record trusted chat, conversation, sub-agent, workflow, phase, and
   invocation attribution.

An inadequate sample or unidentifiable fit SHOULD return explicit model-defined
numeric failure observables such as `sample_valid=0.0`. Infrastructure, policy,
or model-contract failures MUST return a typed failure and MUST NOT fabricate
metrics.

### Result

The result names the study run, its state, and the recorded metric references.
A large parameter or sample value MAY be omitted in favor of an immutable
reference. The completion gate MUST resolve refs and metrics from the durable
job store; it MUST NOT trust copied values in model prose or completion JSON.
The workflow's falsification trials bind every falsifier and control to a
recorded metric reference from a successful or explicitly failed study, never
to a backtest run.

## Hypothesis Development Workflow

The workflow runs discovery -> model creation -> falsification trials ->
verdict -> the strategy chain. Phase wiring is as follows.

| Phase | Kind | Agent behavior | Completion contract and deterministic action |
| --- | --- | --- | --- |
| `discovery` | conversation | Run targeted studies (`validate_study`, `run_study`, `get_job_status`, `get_job_result`, `list_artifacts`, `read_artifact`) on the starting premise, record every attempt including negative results, and identify ONE candidate thesis: signal, information timing, direction, holding period, profit source, expected effect, main competing explanation, entry and exit ideas, necessary assumptions, named building blocks, validation questions, and exploratory evidence. An honest no-candidate conclusion is a valid result. | Construct the discovery completion (`candidate`, `attempts`, `untouchedData`, `limitations`) or the no-candidate completion (`noCandidateReason`, `attempts`, `untouchedData`, `limitations`); advance to model creation with a candidate, or to `completed` when nothing is worth pursuing. A verdict restart re-enters the same durable conversation with only the superseding verdict brief. |
| `model_creation` | conversation | Turn the accepted discovery candidate into ONE statistical model, pre-register the falsifier tests and controls from the knowledge cards — each falsifier carrying a role of claim, assumption, or control — author the research model, and call `validate_study` to compile-check it. Yield so the operator can confirm or revise. After clear confirmation, call `request_phase_completion`. | Construct the model-creation completion; apply falsifiability, model-output, and plan gates; advance to trials or remain. |
| `falsification_trials` | deterministic | `run_falsification_trials` executes the accepted model through the study runner, binds recorded evidence, and returns the trials contract plus the accepted research evidence envelope (per-role assumptions with observations, controls, plan, model and fitted references, report digest); no phase agent or model turn. | Validate the trials and evidence contract and advance to verdict; executor faults or invalid contracts fail the workflow durably. |
| `verdict` | conversation | Interpret the accepted research evidence envelope (per-role assumptions, controls, run states, plan), propose `supported`, `refuted`, or `inconclusive` citing recorded evidence, ask the operator through `request_workflow_input`, and select a target. | Validate the typed verdict contract and the declared transition target; map strategy design to continue, model creation (same premise, revised model) or discovery (premise not worth pursuing) through the transition table. |
| `strategy_design` | conversation | Explain the strategy in prose, ask the operator to confirm or revise, author or patch a committed program, and call `validate_strategy` on its latest ID. | Submit the successful validation's compact program reference; verify committed content and current stored validation, then advance to strategy backtesting. No completion-time compilation or repair. |
| `strategy_backtesting` | one-shot | The owning agent reads the handed-off strategy ID and calls `run_backtest {program_id: ...}` against latest committed content with current accepted validation, then requests phase completion. | Return the actual recorded backtest receipt; advance to optimization on success or loop to strategy RCA on failure. Stale/rejected validation is reported, not bypassed with inline source. |
| `strategy_rca` | one-shot | Inspect the failed run, spawn critics if useful, author a diagnosis and revision guidance, then request completion. | Validate diagnosis and revision refs; loop to strategy design or remain. |
| `optimization` | one-shot | Read the designed strategy's committed parameter declaration, compose a declared set of parameter assignments, submit them in one `run_backtest_batch` call, follow each run to a terminal state, and select a shortlist on net total return, profit factor, drawdown, trade count, turnover/cost sensitivity, and diversity. | Validate the typed candidate-evaluation contract — honest run states and observed metrics only, shortlist vectors canonically matching submitted assignments, a declared selection rule — and fold it into state; advance to null testing, or loop to strategy RCA when the evidence shows a broken design. No retired `run_sweep` MCP surface is implied. |
| `null_testing` | one-shot | Fix the observed run and statistic, declare ordinary permutation or block permutation, signal destruction and preserved properties, a target p-value resolution and a frozen seed list; submit bounded chunks using `run_null_batch` offset/chunk_size, follow each run to a terminal state, and compute `(1 + successful exceedances + failed/nonterminal replicates)/(1 + all declared replicates)`, with selection-conditional scope. | Validate the typed null-test evaluation (observed value matches metrics, every succeeded replicate carries the statistic, exceedance and p-value consistent, unique seeds) and fold it into state; complete, or loop to strategy RCA when the signal does not survive its null. |
| `variation` | one-shot | Compose a frozen deterministic one-at-a-time or declared joint neighborhood around the selected candidate within its declared bounds, submit it in one `run_backtest_batch` call, follow each run to a terminal state, and assess stability from the recorded metrics. | Validate the typed variation evaluation (declared method and interactions, every declared parameter covered, unique vectors, honest metrics) and fold it into state; advance to forward validation, or loop to strategy RCA when the neighborhood is unstable. |
| `forward_validation` | one-shot | Repeat the frozen observed run, null design, and variation points on the protected forward partition (the phase policy rewrites the committed window; tools cannot supply windows) and compare development and forward evidence without retuning. | Validate the typed forward evaluation (nested null and variation evidence with the same cross-checks, forward window recorded) and fold it into state, recording a holdout-look attempt; complete, or loop to strategy RCA when forward evidence contradicts development. |
| `completed` | terminal | None | Terminal outcome with no executor or completion contract. |
| `failed` | terminal | None | Terminal failure with recorded reason and partial evidence. |

Phase agents receive only their phase's permitted tool policy: discovery holds
study execution and inspection tools. Discovery, model creation, and strategy
design share author/update/read/validate/list tools for studies, strategies, and
indicators, plus `read_sdk_reference`; only discovery adds `run_study`.
Discovery is never granted backtest tools: `run_backtest` and
`backtest_config_template` are absent from its policy and its prompt forbids
backtests. The shared SDK authoring guidance is a version-matched authoring
reference for every authoring phase; execution access remains trusted runtime
policy, not something the guidance text grants.
Strategy backtesting is a `OneShotPhase` agent with `read_strategy`, `run_backtest`,
knowledge retrieval, and its phase control tools; it owns its completion request.
`run_backtest` accepts an optional `parameters` assignment validated against the
strategy's committed parameter declaration (omitted names receive declared
defaults; reserved names are rejected); the optimization and variation phases
additionally hold `run_backtest_batch`, and the null-testing and
forward-validation phases hold `run_null_batch` (the same strategy under
seeded signal-destruction transforms); both batch tools validate every point
before any submission and return per-point run ids and states without waiting.
Conversational phases hold
`search_knowledge`/`read_knowledge`. Knowledge retrieval is collection-scoped per phase:
discovery may retrieve `discovery`, `data-quality`, `trading-costs`, and
`model-cards` method/model cards;
model creation only `model-cards`, `inference`, `validation`, and
`trading-costs`; every other phase is unrestricted. The collection allowlist is
trusted runtime policy (a workflow phase absent from the table is unrestricted),
filtered before each source's index is built so lexical, semantic, and
hyperlink-graph expansion cannot reach excluded pages, and re-checked on every
read.

Workflow-scoped data windows come from the research mandate derived from the
durable launch request: the days before the execution cutoff form the
protected forward partition, and every program run is checked against the
development policy at the run boundary (a committed configuration width may
not cross it; model creation and strategy design are also checked at
completion; the trials executor fails before a violating study runs). The
forward-validation phase receives a trusted override policy that rewrites the
committed window to the holdout bounds; agent tools can never supply windows.
Job-backed attempts are recorded at the submission boundary, before dispatch, in the
durable `experiment_ledger` execution phase result: candidate, parameter, variant,
metric, null-test, variation, and holdout-look kinds, one per submitted job. Accepted
completions reference those records and never create them; only the non-job
hypothesis record is still derived from a completion. Acceptance verifies every
reported job against its retained tool job and its submission record, rebuilt and
verified through `AttemptLedger.load`.

### Discovery Completion

The contract hands model creation exactly one candidate and the honest record
of how it was found. It MUST include:

- one candidate with opportunity, signal, information time, direction, holding
  period, profit source, effect, competing explanation, evidence, entry idea,
  exit idea, necessary assumptions, building blocks, validation questions, and
  exploratory evidence; or
- a no-candidate conclusion with an explicit reason when the attempts show
  nothing worth pursuing;
- at least one attempt with purpose, study id, data types, window, and result,
  including attempts that changed nothing; and
- the data deliberately left untouched and the phase's limitations.

A candidate completion advances to `model_creation`; a no-candidate completion
is terminal (`completed`). Discovery never manufactures a candidate to advance
and never treats an honest negative result as a failure.

### Model Creation Completion

The contract MUST preserve the current structured hypothesis, research model,
and falsification-plan fields. It MUST include:

- claim, disagreement, risk factor, horizon, and universe;
- falsifiers with id, observable, condition, threshold, exact test, and role
  (claim, assumption, or control);
- the committed study reference plus description, outputs, and tests, never
  inline Rust or caller paths; the adapter resolves source, dependencies, and
  configuration from the accepted commit. It requires the model's declared window
  (`harnessConfiguration.start`/`end`) and exact
  `harnessConfiguration.subscriptions`, preserved as `data.subscriptions`.
  Entries are strict tagged objects with `data_type` and `symbols`, plus
  `interval` only for bars (`1min` or `1d`), and MUST canonically equal the
  compiled source-and-dependency union. All subscription symbols MUST belong
   to the hypothesis universe. Interval-only configuration is rejected; optional
   global symbols MUST equal the subscription symbol union, and no interval field
   is accepted. The adapter materializes that
  symbol union and the unchanged start/end bounds into the study run; the
  strategy bundle materializes the universe symbols and the same
  window so a strategy backtest never relies on the design agent to declare
  them;
- negative or placebo controls;
- Layer B monitor mappings; and
- a concise operator-confirmation reference to the latest confirming message.

The phase agent asks whether the proposal matches the operator's intent by
ordinary prose and yields. Feedback causes another proposal turn. Confirmation
causes the agent, not keyword-matching workflow code, to submit the completion
contract.

### Falsification Trials Completion

In the current `2.3.0` topology this contract is returned by the deterministic
`run_falsification_trials` executor, not a phase-agent tool call. It runs the
accepted model through the study runner and resolves evidence in deterministic
code. The returned contract carries `trials` plus the accepted research
evidence envelope (`research`) built only from real run outputs: per-role
assumptions and controls with observations and run states, the falsification
plan, model and fitted references, fitted parameters, runtime configuration,
and the report digest. The broader evidence requirements below remain the
design baseline; they do not imply a trials conversation or additional
implemented output receipts.

The contract MUST include:

- hypothesis and research-model refs;
- `run_study` study id and digest;
- fitted-model ref;
- one evidence binding per falsifier;
- one binding per required control;
- observed metric refs, not copied unsupported values;
- interpretation artifact ref; and
- limitations or typed failures.

The executor MUST verify every binding against the recorded study result for
the attempt. A failed harness call is an execution problem, not automatic
hypothesis refutation; executor faults and invalid returned contracts fail the
workflow instead of creating an agent repair turn.

### Verdict Completion

The verdict agent interprets the accepted research evidence envelope and
authors the outcome in prose-first conversation with the operator; the engine
validates the typed contract and the declared transition target, not the
scientific interpretation:

```json
{
  "verdict": {
    "outcome": "supported",
    "reason": "The record and its limitations.",
    "trials": []
  },
  "target": "strategy_design"
}
```

`outcome` is `supported`, `refuted`, or `inconclusive`. Missing evidence,
execution failures, and never-run tests are distinct from a measured falsifier
breach. Legal targets are `strategy_design` (continue), `model_creation` (same
premise, revised model), and `discovery` (premise not worth pursuing).
Ambiguity keeps the agent in the phase; the operator is asked through
`request_workflow_input`, never a keyword classifier.

### Candidate Evaluation Completion

The optimization phase evaluates a bounded parameter search for the designed
strategy. The contract MUST include the validated strategy reference, the batch
identity returned by `run_backtest_batch`, one assignment record per submitted
parameter vector with its run id and honest state (`succeeded`, `failed`,
`running`, or `unknown`) plus observed metrics only when a run produced them, a
shortlist whose parameter vectors canonically match submitted assignments with
a rationale per entry, the declared selection rule, and limitations. Submission
validates every point before launching any; unknown-outcome runs are recorded
truthfully and never relaunched automatically. The fold stores the typed
evaluation in state, and live and replay share the same decoder.

### Robustness Chain Completion

The null-testing contract records the observed statistic and its run, the null
mode and block size, one record per seeded replicate with honest states and
observed metrics only, the exceedance count, and the empirical
`(1+k+failed)/(1+all)` conservative p-value; the validator recomputes exceedance and p from the
recorded values. The variation contract records the selected center and the
deterministic marginal or declared joint neighborhood without a point ceiling, with honest
states, metrics only when runs produced them, the varied parameter names, and a
stability assessment; the phase report projects the same payload for the UI
charts. The forward-validation contract repeats both designs on the protected
forward partition and records the forward window; it must not re-select
parameters from forward results. Job submissions in this chain append exposure
records at submission (null-test, variation, and holdout-look kinds included), and
accepted completions reference them. The frozen research design
(`hypothesis_development/design.py`) is derived from the accepted development
evidence, and its digest is stamped on every forward submission. Forward validation
(`provenance.py`) rejects a reported job that did not run the frozen strategy, the
reported parameters, the forward window, and the frozen design; an executed forward
job left unreported; and a reported metric the harness did not produce.

## Recovery and Idempotency

**REQ-014: Write before dispatch.** An assistant tool-call turn and pending
tool invocation MUST be durable before transport dispatch.

**REQ-015: Idempotent tools.** Side-effecting tools MUST deduplicate by trusted
idempotency key. Recovery after a crash MUST return the recorded terminal result
instead of repeating a fit, backtest, sweep, write, activation, or child spawn.

**REQ-016: Resume from durable boundary.** Recovery MUST reconstruct the
sub-agent conversation from durable turns, restore the latest compatible
checkpoint, and resume only unfinished invocations.

**REQ-017: Stale intent.** Operator steering received during a tool call MUST
be persisted immediately. Before applying a completion request or transition,
the workflow MUST detect stale intent and re-enter the phase with the new
operator input rather than advancing under obsolete assumptions.

## Chat Surface, Anchoring, and Datum Rendering

These requirements extend the tool architecture above and are normative for the
migration recorded in `thesis-chat-surface-and-subagent-anchoring-v1`.

**REQ-018: Sub-agent parity.** A sub-agent MUST use the same chat surface and
piping as the top-level agent, nested one level down. Transcript seeding, turn
persistence, datum projection, streaming, and operator input MUST be one
implementation parameterized by anchor. The only permitted differences are the
anchor, the tool policy, and the lifecycle that creates, drives, and terminates
the agent.

- Rationale: a sub-agent is the same conversation abstraction, not a second class
  of agent.
- Verification: the same code path serves a root turn and a node turn, and a
  parity test asserts identical persistence and projection for both.

**REQ-019: Caller anchor decides placement.** Every turn and datum MUST render at
the anchor of the agent that produced it. There MUST NOT be a per-tool rendering
exception, a per-tool suppression rule, or a tiebreaker between the caller anchor
and record ownership.

- Rationale: one uniform rule; nesting then falls out without special cases.
- Verification: a workflow launched by a sub-agent renders entirely inside that
  sub-agent, identically to a root launch.

**REQ-020: Typed datum contract.** A tool MUST return one or more typed datums.
Each datum MUST carry its declared type, and the type MUST be persisted with the
datum so rendering survives reload. The shared surface MUST select the canonical
rendering from the datum type, with the generic tool card as the fallback. A tool
MUST NOT carry rendering instructions, and no tool may be special-cased by name. A
single tool call MAY return several datums of different types asynchronously.

- Rationale: the contract is the type; the surface owns rendering.
- Verification: a multi-datum tool renders each datum by its own type; an unknown
  type falls back to the generic card.

**REQ-021: Anchor-owned executions.** A workflow execution MUST be owned by the
anchor that launched it. Execution-scoped routes MUST be anchor-qualified, and a
chat listing MUST NOT include executions launched by its sub-agents.

- Rationale: execution ownership and card placement must agree, or a
  node-launched execution can never match root-launched parity.
- Verification: a node-launched execution is retrievable and continuable under
  the node, and is absent from the chat-level list.

**REQ-022: Durable workflow node.** Every workflow execution MUST have a durable
workflow-node record created by, and parented to, the launching anchor. Phase
agents MUST be children of that workflow node, so the chain is launching agent →
workflow node → phase agents and the existing depth, active-children, and budget
guards apply to phase agents.

- Rationale: sub-agents belong to the execution, and the execution belongs to the
  parent.
- Verification: a phase agent's durable record resolves to the workflow node, and
  the guards reject an over-deep or over-budget phase spawn.

**REQ-023: Agent-first transitions.** A phase transition MUST be requested by
that phase's executor — a sub-agent, or deterministic phase code where the phase
is deterministic — through the tools its allowed structure exposes. The engine
MUST validate the request against the transition table and apply it; the engine
MUST NOT pause or resume an execution for operator input, and there MUST NOT be a
workflow-level resume path. A completion request MUST stamp the intent epoch it
was derived from, and a request stamped with an epoch that has since advanced
MUST be rejected in-turn so the executor re-derives.

- Rationale: the executor owns the conversation; the engine owns legality and
  routing. The epoch is a read-stamp for delayed or replayed artifacts, not a
  gate on conversations.
- Verification: a phase asks for operator input and completes without fencing;
  the operator's answer drives the phase turn that requests the transition; a
  stale-epoch request is rejected and re-derived.

**REQ-024: Per-node operator policy.** Answering a node's question MUST be
governed by a per-node operator policy that can authorize the human operator, a
parent agent, or another agent. The default MUST be human-only. The answering
actor MUST be derived from the authenticated session and MUST NOT be accepted from
model arguments or the request body. The operator gate MUST be prompted, not
structurally enforced: the phase prompt instructs the executor to request operator
input where the structure requires it, and the engine trusts the prompt rather
than gating the transition.

- Rationale: autonomy becomes a policy change rather than a structural change,
  attribution stays trustworthy, and a model that ignores its prompts fails
  visibly instead of being silently distrusted.
- Verification: a node whose policy is human-only rejects an agent answer; a node
  whose policy authorizes a parent accepts one with provenance; a transition
  after an operator answer carries the authenticated actor.

**REQ-025: Single delivery path.** Delivering a datum to a conversation MUST use
one path parameterized by anchor: persist the datum at the anchor, then schedule a
turn at that anchor. Delivery MUST be durable before it is scheduled and MUST be
idempotent under retry. A delivery MAY persist without scheduling when the
continuation has exactly one scheduling point (the accepted operator answer). A
parent-bound result MUST NOT be delivered only through an execution event.

- Rationale: one mechanism for roots and nodes; no second-class delivery and no
  redundant scheduled turn.
- Verification: a retried delivery does not duplicate the datum or the turn; one
  launch → question → answer schedules exactly one turn.

**REQ-026: One general workflow implementation.** A workflow MUST be a
`WorkflowSpec`: an id, a version, an ordered set of phases, the allowed
transitions between them, a per-phase seed (the prompt that prepares that
phase's agent) and validator (its completion contract), the piping between
phases, and the workflow-start handoff adapter. The general implementation
(spec, builder, registry, machine, delivery plumbing, and handoff and completion
application) MUST live under `workflows/core/` and MUST contain no
workflow-specific behavior or contract. A phase agent owns its turns and
advances the workflow only by calling the completion tool; a deterministic phase
may declare an executor instead of an agent seed.

- Rationale: one engine, many workflows; the hypothesis workflow is a
  configuration of the engine, not the engine itself.
- Verification: a second workflow can be registered and executed with no edits to
  `workflows/core/`.

**REQ-027: Builder and registry.** A `WorkflowBuilder` MUST assemble a spec from
its parts and validate it (a phase declares a seed or an executor, `on_complete`
names a declared transition target, every transition is in the table, terminal
phases exist). A `WorkflowRegistry` MUST resolve a workflow id to its spec, and
the workflow tool MUST dispatch by registry lookup rather than a named
per-workflow method. `execute` MUST be the single generic entry point.

- Rationale: construction and dispatch are data, not code branches.
- Verification: an unknown id is rejected by the registry; adding a workflow
  requires only registration.

**REQ-028: Handoffs are explicit and transcripts are private.** Cross-phase data
movement MUST flow only through the accepted completion contract and the declared
piping adapters; an agent MUST NOT read another agent's transcript. The
workflow-start adapter threads the caller's message into the first phase.
Continuation MUST rely on the durable transcript plus the artifacts it
references; there MUST NOT be a workflow state codec, checkpoint store, or
execution-level resume path.

- Rationale: a phase's inputs are explicit in its seed, and durability is the
  conversation itself.
- Verification: a fresh workflow object reseeds the next phase from the accepted
  contract and referenced artifacts alone.

**REQ-029: Concrete workflows are self-contained packages.** Each concrete
workflow MUST live under `workflows/registry/<workflow-id>/` with its phases,
contracts, gates, prompts, seeds, and phase modules, and MUST NOT be imported by
another workflow.

- Rationale: partitioning makes workflows independently replaceable and keeps
  `core/` stable.
- Verification: the hypothesis workflow imports nothing from
  `workflows/registry/**` other than its own package.

**REQ-030: Spawning is prompting.** Creating an agent and giving it its first
prompt MUST be one durable operation: the spawn creates the record and delivers
the first prompt at the agent's own anchor, and that delivery is what schedules
the agent's own turn. There MUST NOT be a path that creates an agent and then
separately schedules its first turn, and the workflow MUST NOT call a scheduler
to start an agent.

- Rationale: "prompted" and "started" are the same act; splitting them leaves a
  window in which an agent exists with nothing to do.
- Verification: an agent whose prompt is delivered is scheduled exactly once;
  no workflow code calls a turn scheduler.

**REQ-031: Deterministic phases request transitions too.** A phase MAY declare
`kind=deterministic` with an executor. When the engine enters such a phase it
MUST call the executor for a contract and validate that contract exactly as an
agent completion (validator, required fields, target); an invalid contract is
an engine error, and a valid one advances and cascades through consecutive
deterministic phases. A phase MUST declare either a seed or an executor, and a
deterministic phase MUST declare an executor; the builder MUST reject
violations.

- Rationale: a phase's executor may be an agent invocation or deterministic
  code; both request the same contract-shaped transition.
- Verification: a deterministic phase's contract is validated and applied with
  no agent and no model call; a deterministic phase without an executor is
  rejected at build time.

**REQ-032: One turn worker per node.** Turns for a node MUST be serialized by a
single worker per node. A wake that arrives while a turn is running MUST
coalesce into exactly one follow-up turn — never a concurrent turn on the same
node, never a dropped wake — and the running turn MUST drain new inbound
messages between model iterations so the model sees the latest data without
restarting the turn. A text-only turn for a `complete_on_text` child MUST complete
the node with its answer and deliver it to the caller; a conversation agent MUST
accept input while it runs; a phase agent MUST complete only through the
completion tool.

- Rationale: the transcript is the mailbox; replay plus coalescing preserves all
  work, and draining keeps latency low without discarding anything.
- Verification: concurrent wakes produce one running turn and one follow-up
  that sees them all; a message arriving mid-turn is visible to the next model
  iteration.

**REQ-033: Asynchronous results and bounded await.** A child's terminal result
MUST be delivered at its caller's anchor and schedule the caller's turn, except
when the caller is a workflow node — a durable record that MUST never be
scheduled or turned. `await_sub_agent` MUST wait for the child for a bounded
time and return its status and result; on timeout it MUST return the current
state rather than blocking indefinitely.

- Rationale: waiting is optional because delivery wakes the caller; the tool is
  named await, so it must actually wait when called.
- Verification: a completed child wakes its caller without an await; an await
  returns the terminal result when the child finishes inside the window and the
  current state at the timeout.

**REQ-034: One conversation abstraction.** Chats and sub-agents MUST share
one implementation of internals (identity, transcript, send/deliver/answer,
status, turn execution). A chat MUST NOT be a subclass with different
behavior: it is a conversation at the root anchor, and the root turn path MUST
be the conversation's turn implementation. Target design:
`thesis-conversation-abstraction-v1`.

- Rationale: two turn paths for the same concept is the drift this repository
  keeps paying for.
- Verification: a root-conversation turn and a node turn run through one
  implementation; chat behavior is unchanged from the wire's view.

**REQ-035: Child semantics are composed, not inherited.** A child MUST
delegate every internal to its inner conversation and add only the return
target, `complete`/`fail`/`result`/`await_result`/`children`, cancellation,
and one turn-end hook ("does a text-ending turn complete me?"). A wrapper
that implements turns, identity, or persistence is a defect.

- Rationale: the return layer is additive; internals are the invariant.
- Verification: child semantics are testable without a root; delegation
  covers every other operation.

**REQ-036: The return target is an anchor, not a conversation.** Delivery at
a conversation anchor schedules that conversation's turn; delivery at a record
anchor is durable-only because the engine already reacted. The same child
wrapper MUST serve chat-spawned helpers and phase agents without
special-casing either.

- Rationale: phase agents return to the workflow node, which is a record.
- Verification: one wrapper serves both; record-anchor delivery schedules
  nothing.

**REQ-037: Independent durable lifecycle policy.** One-shot versus conversational
kind MUST govern unsolicited input acceptance, never a different implementation
of internals. Durable completion mode MUST independently select the turn-end
policy; `complete_on_text` children complete on their final answer and deliver to
their durable return anchor, while `explicit_completion` children stay open.
Phase executors MUST use explicit completion and explicit phase authority.
Legacy inference MUST remain confined to persistence decoding.

- Rationale: input acceptance, completion policy, return placement, and workflow
  authority are distinct durable facts, not inferred from parentage or phase text.
- Verification: reconstructing a handle selects policy from completion mode and
  preserves return anchor and authority independently of input kind.

**REQ-038: No phase knowledge in child semantics.** Phase binding is spec
data on the record, the completion tool the engine binds into the child's
registry, and the return anchor supplied at spawn. The conversation hierarchy
MUST contain no phase vocabulary.

- Rationale: phases are engine-side bindings, not a conversation concern.
- Verification: the child classes contain no phase names or contracts.

**REQ-039: The service owns scheduling.** Every wake is a delivery; the
`Conversations` service owns the per-conversation workers, the routing rule
(root → itself; record anchor → durable-only; node → worker), and spawning.
Upstream MUST NOT call a scheduler, runner, or coordinator.

- Rationale: upstream seeing machinery is the miss this closes.
- Verification: no code outside the service references the coordinator or
  runner.

**REQ-040: Phase specs are a sealed union of objects.** A workflow's phases
MUST be a sealed union of frozen dataclasses: conversation, one-shot,
deterministic, and terminal. They are dispatched by exhaustive match; invalid
combinations MUST be
unrepresentable rather than builder-rejected. An agent phase's spec produces a
child conversation through the service; deterministic and terminal phases
produce none.

- Rationale: the flat spec's optional fields are validated combinations; types
  make them unrepresentable.
- Verification: adding a phase type fails to compile unmatched sites.

Anti-patterns observed during this migration — turn drivers, engine-side model
calls, sibling-transcript reads, re-embedding conversations in seeds,
prose-only configuration fields, delivered tool messages without envelopes,
silently invented entities, and a second launch entry point — are catalogued in
the authoring guide `thesis-workflow-authoring-guide`. REQ-034..REQ-040 are the
conversation abstraction recorded in
`thesis-conversation-abstraction-v1`; they are implemented by
`thesis.agent.runtime.conversation` and `thesis.agent.runtime.child` (one
conversation hierarchy, composed child semantics, anchor-based returns, the
independent durable completion/input policies, the service-owned routing rule,
and the sealed phase-spec union).

**REQ-041: Contract handoffs are validated before they commit and advertised
before they are called.** A phase's completion contract MUST be validated
completely — every missing or wrongly-typed field reported once, in one
deterministic pass — before the engine applies the transition, so a rejected
handoff can never leave the execution committed to a phase whose successor
cannot consume the contract. A contract the deterministic successor composes
(the hypothesis-development creation handoff) MUST be validated by the same
shape the composer enforces, guarded by a re-run of the composer itself so the
validator and the consumer cannot drift. Every rejection MUST travel the
`PhaseCompletionRejected` repair channel as an ordinary tool result, never as
a generic fault. A phase whose spec declares a completion JSON Schema MUST
have it advertised on the `request_phase_completion` tool its agent sees, so
the provider constrains the call at decode time; the engine still validates
the accepted call deterministically. Tool-call arguments that are not a bare
JSON object MUST be recovered tolerantly (fenced or embedded JSON) before the
call fails, and an unrecoverable call MUST fail with an actionable message.

- Rationale: a handoff that fails deep validation after the transition
  commits wedges the execution in a phase no live agent can complete, and a
  first-error-only rejection forces the model into blind iteration; a
  schema-constrained, fully-reported contract repairs in one turn. A phase
  agent MUST never be invited to end the run itself: the completion
  instruction and the advertised target enum name only real successors,
  never the `failed` phase — an agent that cannot proceed asks the operator
  through `request_workflow_input` instead. Operator gates stay
  conversational; per the workflow development instructions, no deterministic
  gate may be added to a workflow unless the operator explicitly requested
  it.
- Verification: a deeply invalid creation contract is rejected in-turn with
  every problem listed, the execution stays in its current phase, and the
  repaired contract completes the workflow; the phase agent's tool catalog
  carries the declared schema; fenced and embedded tool arguments dispatch
  normally.

## Implemented Package Layout

The active boundaries are below. `__init__.py` files expose intentional public
facades. This is not a claim that every earlier migration requirement or
repair-budget mechanism is complete.

| Path | Implemented responsibility |
| --- | --- |
| `agent/runtime/__init__.py` | Public conversation, child, session, lifecycle, control, delivery, and store facade. |
| `agent/runtime/conversation.py` | Root/child `run_node_turn`, shared history decoding and `_persist_turn`, runtime-internal activity, and anchored intake. |
| `agent/runtime/session.py` | Durable model/tool loop and session turn/result contracts. |
| `agent/runtime/turns.py` | Per-conversation generations/positions, bounded recent-ID memory, durable high-water ACKs, and safe idle retirement. |
| `agent/runtime/activity.py` | Discriminated immutable activity, source-generated activity/message schemas, and the canonical activity decoder. |
| `agent/runtime/child.py`, `lifecycle.py`, `delivery.py` | Child return policy, durable lifecycle, and one anchored datum delivery path. |
| `agent/runtime/control.py`, `control_tools.py`, `registry.py` | Control commands, schemas/handlers, and merged tool catalog. |
| `agent/runtime/knowledge_tools.py`, `program_tools.py`, `strategy_tools.py`, `workflow_tools.py` | Context-bound retrieval, committed program tools, legacy strategy-domain authoring, and workflow launch adapters. Program binding replaces conflicting legacy/raw harness tools. |
| `agent/runtime/context.py`, `datum.py`, `specs.py`, `policy.py`, `store.py`, `tree.py`, `spawn.py`, `schema.py`, `json_recovery.py` | Trusted attribution, typed datums/specs, admission, persistence, hierarchy/spawn, and argument construction. |
| `agent/harness_tools.py` | MCP catalog/result adapter, not a validation cache. |
| `workflows/core/` | Generic spec, phase, builder, registry, machine, workflow, state, execution store, delivery, and events; `workflow.py` accepts the reducer port. |
| `workflows/registry/hypothesis_development/__init__.py` | Concrete workflow public facade, including `fold_transition` and `rebuild_state`. |
| `hypothesis_development/spec.py`, `doctrine.py`, `piping.py` | One phase declaration for topology/policy/payload folds, domain ports/reducer binding, and accepted handoffs applied once. |
| `hypothesis_development/session_services.py` | Session role policy, schema/knowledge/authoring bindings, validation/application, and failure callback. |
| `hypothesis_development/services.py`, `rebuild.py`, `state.py`, `artifacts.py`, `exposure.py`, `design.py`, `provenance.py`, `jobs.py`, `errors.py`, `primitives.py` | Domain validation/evidence, shared live/replay fold, state, artifacts, submission exposure ledger, the frozen research design, completion provenance checks, jobs, and domain values. |
| `hypothesis_development/phases/`, `contracts/`, `gates/`, `prompts/`, `composers/` | Phase declarations, typed completion/validation, domain acceptance, agent instructions, and construction. Discovery has its own contract/prompt; `prompts/model_creation.py` replaces the retired creation prompt. Domain acceptance modules are not operator workflow gates. |
| `workflows/runtime.py` | Composition and exported typed workflow session ports. |
| `workflows/local.py` | MCP runners, bounded polling, and local composition. |
| `http/application.py`, `errors.py` | Same-app root/API composition, fresh factory defaults, fail-closed service resolution, and typed error translation. |
| `http/schema.py`, `wire.py` | Authoritative OpenAPI/runtime export and source-derived response/record models. |
| `http/timeline.py`, `streaming.py` | Unified chat timeline REST history and the sole conversation SSE route, with shared resume-cursor transport primitives. |
| `research/hypothesis.py` | Workspace repository-backed immutable canonical hypothesis versions, journaled fenced publication, and committed-content verification. |
| `workspaces/programs.py` | Committed study/strategy/indicator source and manifests, content/dependency versions, exact-input validation receipts, ID-based execution, SDK references/templates, and pinned snapshot loading. |
| `workflows/research_models.py` | `ProgramSnapshots` read port and accepted study-reference resolution for deterministic research execution. |
| `hypothesis_development/prompts/sdk.py` | Shared SDK lookup, targeted patch, validation, and prompt-only repair guidance. |
| `cosmos/chunks.py`, `records.py`, `timeline.py` | Immutable bounded payload trees, forced opaque provider-request chunk storage before invocation persistence, the single authoritative conversation store with atomic sequence commits, and verified hydration. |

`agent/session_reply.py` and the standalone strategy-development, discovery,
and RCA workflow packages are removed. Concrete workflows MUST live under
`workflows/registry/<workflow-id>/` and MUST be resolved by id through
`core/registry.py`; no workflow-specific behavior or contract may live in
`core/`. The tool surface resolves a requested workflow id to its spec and calls
the generic `execute`.

`src/thesis/execution/checkpoints.py` is not the workflow state reducer. The
execution event log remains durable event evidence and supports REST replay;
its events project into the unified chat timeline for live streaming.
First-class invocation and sub-agent stores remain their own durable sources
of truth. There is no separate execution SSE delivery path.

Tests MUST mirror the package tree. The monolithic hypothesis-development test
module MUST be split into core protocol, phase, gate, tool, recovery, and
end-to-end suites.

## Migration Plan

### Wave 1: Core Contracts

- Add `AgentSpec`, `PhaseSpec`, phase kinds, gate outcomes, completion
  contracts, and durable invocation/sub-agent store protocols.
- Add fakes and contract tests without changing runtime behavior.

### Wave 2: Durable Agent Session

- Unify the current chat tool loop and workflow model router behind
  `AgentSession`.
- Persist every model and tool invocation and every conversation turn.
- Add crash recovery and idempotency tests.

### Wave 3: Sub-Agent Tools

- Implement `spawn_sub_agent`, `await_sub_agent`,
  `request_sub_agent_completion`, and `request_operator_input`.
- Route both chat and workflow phase spawning through the same command handler.
- Add depth, child-count, budget-inheritance, and authorization tests.

### Wave 4: Harness Research Tool

- Publish `run_study` in quant-harness MCP.
- Add trusted attribution and idempotency to harness calls.
- Wire the Thesis harness adapter and durable evidence resolution.

### Wave 5: Phase Completion

- Implement phase-specific `request_phase_completion` registration,
  construction feedback, two repair turns, deterministic gates, and operator
  escalation.
- Remove the `advance` boolean, keyword verdict classifier, and synthetic tool
  events.

### Wave 6: Hypothesis Development Package

- Move contracts, prompts, gates, phases, artifacts, and loops into the nested
  package one concern at a time.
- Preserve the existing public facade and operator UX throughout migration.
- Delete the monolithic module only after parity and recovery tests pass.

### Wave 7: Sibling Workflows

- Migrate strategy development, discovery, and RCA to the same phase runtime.
- Remove workflow-specific model/tool loops and duplicated transition plumbing.

## Constraints

| ID | Constraint | Enforcement |
| --- | --- | --- |
| CON-001 | The state machine, not a model, owns legal transitions. | Transition table validation. |
| CON-002 | A phase-root completion tool cannot be called by a child helper. | Trusted caller identity and phase-root check. |
| CON-003 | Tool attribution fields cannot be model-controlled. | Out-of-band `ToolContext`. |
| CON-004 | Recorded evidence must come from a tool result associated with the active phase attempt, unless explicitly linked by policy. | Acceptance-gate evidence resolver. |
| CON-005 | No side effect may repeat after recovery. | Durable invocation state and idempotency keys. |
| CON-006 | A conversation accepts mid-turn input, but active provider calls are immutable. | Durable input queue consumed at turn boundaries. |
| CON-007 | A one-shot phase accepts operator input only after requesting it. | Sub-agent `accepts_input` state. |
| CON-008 | Construction repair is three attempts per turn and two repair turns per phase before operator escalation. | Agent-session budget state. |
| CON-009 | Dynamic sub-agent spawning cannot escape chat/workspace scope or delegated budget. | Spawn command validation. |
| CON-010 | Research phases cannot access activation-grade tools. | Per-sub-agent default-deny policies. |

## Compliance

An implementation complies with this specification only if all of the
following are true:

1. Agent-backed phases run on independently durable, chat-attached sub-agents.
2. Models can directly call permitted harness MCP tools and receive durable
   tool results in their own conversations.
3. Models request phase completion through `request_phase_completion`; no
   model output directly mutates workflow phase state.
4. Three same-turn construction failures produce structured model feedback;
   two failed repair turns produce operator input escalation.
5. Acceptance gates resolve model-submitted refs against durable evidence and
   alone select legal state-machine actions.
6. Conversation phases accept operator messages while running; one-shot phases
   do so only after requesting input.
7. Sub-agents can be spawned from chat or workflow agents through the same
   durable tool/command contract.
8. Every invocation and turn survives process restart and is replayed in the
   same order without repeating completed side effects.
9. Hypothesis creation and verdict retain their current visible interaction:
   propose/review/confirm and verdict/review/continue-or-loop-back.
10. The implementation follows the nested package boundaries in this document
    or documents an equivalent decomposition with no replacement monolith.


Research protocol launch requires explicit history/forward lengths and declared
information, position, and liquidation horizons. Purge is the maximum of the
information and position horizons; liquidation determines the embargo. These
choices are persisted, verified on recovery, and never replaced by defaults.
Each execution window begins from clean strategy/account state; the launch
horizons are research commitments, not static proof of arbitrary authored code.
Frozen forward design includes the exact center and variation vectors, null
method, all seeds, target resolution, statistic, signal adequacy, and failure
policy. Ordinary permutation destroys serial order; block permutation destroys
inter-block order while retaining within-block dependence. Calendar volatility
schedule preservation is unsupported by these transforms and must not be
claimed. Search-adjusted inference is unsupported; selection-conditional
limitations remain explicit. Null submissions paginate one full frozen seed
list with stable batch identity, never automatically retrying unknown outcomes.

Development and forward assessments verify the authoritative harness job ID,
state, reported metrics, immutable accepted input and retained submission digest.
The submission ledger remains the sole data exposure authority. Forward reports
export the frozen and launch designs and the submission ledger; earlier forward
designs on the same holdout downgrade the report to adaptive holdout reuse.
Reports share immutable accepted evidence tables, including failed, missing,
undefined and untested states, marginal and declared joint parameter views,
matched development/forward comparisons and job/artifact inspection. UI fixtures
are produced by backend contract composition.

The runtime requires an explicitly supplied execution runner. The former HTTP-client fallback, unused validator/journal/subagent-factory dependencies, retired phase aliases, and inline-study replay are removed. Replay requires committed study references and complete accepted research evidence.
