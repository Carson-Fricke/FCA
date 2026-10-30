# Thesis Durable Agent and Workflow Runtime

## Runtime and FCA extension boundary

The generic host is `src/thesis_runtime/`. It has no imports from `thesis` and
owns neutral messages, conversations, provider-independent sessions, tool
dispatch, subagent lifecycle, workflow execution, durable work, and storage
ownership. `src/thesis/application/` chooses providers, persistence adapters,
FCA prompts, policies, extensions, and deployment composition. Dependencies
point from FCA into the runtime. Runtime inner layers depend on contracts and
ports; only composition selects PostgreSQL, Cosmos, filesystem, and native model adapters.

The HTTP factory reuses service-independent router declarations. Authentication,
services, workflow registrations and stream configuration remain local to each
application; the declaration cache owns no operator or execution state.

`ChatService.list_tool_jobs(workspace_id)` exposes neutral retained operation
references to product read models. FCA classifies harness operations outside the
runtime and reads execution state through its injected harness port. These reads
do not dispatch tools, scan transcripts, or create another incoming delivery path.

```text
src/
  thesis_runtime/
    contracts/       Neutral messages, records, context, datums, errors, codecs
    conversations/   Canonical intake, inbox, handles, durable conversation service
    sessions/        Provider-independent model and tool loop
    subagents/       Lifecycle, delegation, completion and operator controls
    tools/           Catalog, argument validation, launch and hosted-tool ports
    workflows/       Generic topology, execution, registry and extension protocol
    execution/       Events, durable runnable work and worker claims
    storage/         Ownership ports and local mutation locks
    hosting/         Generic worker handlers and version-pinned recovery
    adapters/        PostgreSQL, Cosmos, filesystem and native model providers
  thesis/
    application/     FCA composition, launcher, prompts and domain projections
    extensions/
      tools/         Workspace, program, sketch, knowledge and harness bindings
      workflows/     FCA session bindings, hypothesis development and research investigations
    research/        Hypotheses, investigations and research models
    persistence/     FCA investigation and retained submission stores
    backtests/       Retained execution discovery and published report records
    workspaces/      Git and workspace artifacts
    http/            FCA HTTP transport and generated UI contracts
```

Tools expose provider-neutral descriptors and receive model arguments separately
from trusted `ToolContext` on every dispatch. The host supplies invocation and
idempotency identity, actor, scope, epoch, and effective policy. Native function
wrappers belong to the provider adapter. `tools/remote.py` defines a versioned
JSON transport port with exact invocation acknowledgement; interrupted or
mismatched replies produce unknown outcomes without automatic replay. FCA tools
currently execute locally; an MCP transport implementation is future work.

`adapters/providers/families/` owns pure tool presentation and call decoding.
FCA composition selects a family once from the configured model namespace and
injects it into the shared `NativeSessionAdapter`, including workflow model
calls. DeepSeek is the only specialized implementation: object-root parameter
properties expose types while the original `oneOf` retains branch coupling,
required fields, references, and extra-property rejection. Its provider-facing
schemas expand the nonblank substring regex `\S` to `^[\s\S]*\S[\s\S]*$`,
preserving accepted values for endpoints whose constrained generators require
whole-string matches. Traversal visits schema positions, including nested
objects, arrays, unions and definitions; literal defaults/examples/constants
and unrelated regexes retain their meaning. Tool names and raw
arguments are unchanged; canonical dispatch remains the validation authority.
Other families use shared native tool behavior. Adding a family requires one
pure implementation, a resolver entry, and provider-exchange behavior tests.
Families do not own transport, persistence, incoming delivery, or session state.

The same request projection prepares live calls and replay audits, recording
family, adapter version, and the digest of the actual advertised catalog.
Provider response identity (upstream provider and generation ID when supplied)
is retained as neutral outgoing metadata. The mandatory delivery source guard
requires the family modules and checks every Python module in that package for
adapter purity.

The registry holds one current registration per workflow ID. Launches validate
the extension-owned input schema; recovery checks the durable version against
that registration and rejects a mismatch as outdated. Duplicate IDs fail even
when the supplied version differs. Historical implementations are not retained,
and current definitions are never substituted for outdated executions.
FCA registrations retain their recovery callback for both queued initial phase
entry and restart recovery. Execution-specific recovery faults are logged and
retained as deduplicated `workflow.progress` diagnostics, without completing a
phase or replaying interrupted mutations. Only explicitly replay-safe deterministic activities may be recomputed. A failed repair pass does not terminate
the durable worker's queued-work loop.
`workflows/extensions.py` defines the versioned JSON endpoint for launch parsing,
state folding, seed text, validation, deterministic execution, and reporting.
Even local calls cross detached JSON values. The engine receives structural state
and opaque payloads; no native FCA request, service bundle, callback, or engine
object crosses that endpoint. Terminal outcomes are declared in phase metadata.
The host validates trusted execution identity and projected phase/epoch before
accepting a transition. Accepted history is committed by revision CAS under the
execution ownership fence; effects follow acceptance.

The FCA endpoint owns its services and uses execution-scoped
`DevelopmentActivities` for artifact and job operations. Domain phases do not
inspect workflow-engine internals or write accepted transition history.
Session policy and completion capabilities live in FCA extension bindings.

Runtime records carry opaque namespaced `references` rather than fixed strategy,
experiment, or investigation fields. FCA owns `fca.strategy`, `fca.experiment`,
and `fca.investigation`, including investigation CRUD and harness submission
retention. Domain documents use separate collection scopes in the selected
backend (separate Cosmos containers or PostgreSQL collection keys); the generic
conversation catalog does not interpret them. HTTP request models remain FCA
models, while response schemas and UI decoding use the neutral reference shape.

The pure record codec lives in `contracts/codec.py`; Cosmos does not import the
filesystem adapter. Provider failure categories live in neutral contracts.
`contracts/failures.py` owns the lossless tool-failure value and exception carrier.
Failed tool outcomes cannot omit a nonempty category and explanation. The common
dispatcher preserves typed domain evidence, including recovery arguments, through
the existing canonical inbox and pure provider projection. No diagnostic queue or
alternate delivery path is introduced.
`ChatService.finish_workflow` is the fenced terminal-state authority and rejects
late terminal overwrites; record and event writes share the fence but are not
claimed to be a cross-record transaction.

API replicas persist input and runnable references. Generic durable workers own
renewable, fenced claims, so correctness does not depend on one replica. Local
startup embeds the same worker. This change establishes Python package and JSON
boundaries for a future Rust runtime; it does not implement a Rust service or
deploy remote tools.

`tests/thesis_runtime/test_package_boundaries.py` enforces dependency direction.
`test_extension_host.py` runs an unrelated document workflow and hosted tool
through the real host, including pinned-version recovery, acknowledgement
mismatch, and interrupted dispatch. Mandatory delivery, ownership, job identity,
and application behavior suites remain required.

The key words "MUST", "MUST NOT", "REQUIRED", "SHALL", "SHOULD",
"SHOULD NOT", "RECOMMENDED", "MAY", and "OPTIONAL" in this document are to
be interpreted as described in RFC 2119.

Hypothesis-development v4 launches from required `input.idea` and an optional
investigation link. The root preserves the request and launches directly without
knowledge retrieval, sketch inspection or research-design choices. Discovery
interprets dates against the persisted launch timestamp and calls its scoped
`set_research_window` tool before empirical work. The explicit development and
forward windows are frozen in `ExecutionDocument.phase_results["research_window"]`
using existing execution acceptance and revision checks. Program capabilities
are absent until declaration, then use the existing phase-window enforcement;
forward validation receives only the sealed holdout policy. Helpers cannot
write the declaration. Signal lookbacks, warm-up, label purges and liquidation
rules remain design choices within the windows and do not move dataset dates.
`WorkflowSessionBindings.tools` carries extension-owned tools into the ordinary
session registry; no workflow-specific policy is added to the generic host.
The old launch day-count/dependency contract and derivation are removed. Old
workflow versions and mandate documents are rejected rather than upgraded or
resumed. Source changes do not delete unrelated local executions; affected old
executions must be retired during the deployment cutover. Behavior is verified
by `test_launch_schema.py`, `test_research_mandate.py` and the study-validation
HTTP suite; pure window/schema checks live in `tests/thesis/experiments/test_mandate.py`.

## Context checkpoints

Root, helper and phase sessions use the same complete canonical context window.
There is no 40-message buffer. `ConversationTurn` reads every scoped message after
`ContextCheckpoint.through_message_id`, prepends the checkpoint's canonical
summary and retained evidence references, and supplies the current configured
bootstrap instructions. Superseded compaction summaries are control artifacts,
not additional task history. The complete transcript is retained for inspection.
`ContextCheckpoint` is a new optional selector record; existing current canonical
transcripts require no conversion or deletion and start with complete history.
Outdated canonical/provider contracts continue to be rejected, never upgraded.

`contracts/compaction.py` defines capacity budgets and the optional evidence
ranking port. `sessions/compaction.py` owns pure token estimation, consumed-prefix
cuts and complete correlated group selection. `sessions/context_manager.py` owns
the intrinsic `compact_context(focus?)` tool, capacity backstop, usage notices,
audited same-model summary requests and window installation. Application
composition supplies the model capacity and optional TypeSafe Jev adapter.
No runtime component interprets research progress or decides important job IDs.
The summary instructions ask the working model to preserve the assignment,
corrections, decisions, progress, unresolved evidence and next steps.

Each request is estimated from its full pure provider snapshot, including current
instructions, tools and opaque continuation; reply and safety capacity are
reserved separately. Estimates are explicitly approximate. The default serving
DeepSeek window is 1,000,000 tokens; other models default to 128,000 unless
`THESIS_CONTEXT_WINDOW_TOKENS` is configured. `THESIS_MAX_OUTPUT_TOKENS` controls
both the OpenRouter output cap and runtime output reserve, including reasoning
(default 131,072 for the serving DeepSeek model, 32,768 for other models).
Usage notices begin at 50% and update in 5% bands. They use the canonical incoming
writer and force reconciliation before a provider request, preventing a later
notice position from acknowledging an unseen intervening arrival.

Compaction summarizes the original retiring span with the same provider/model,
using dedicated checkpoint instructions. The shared pure provider projection
encodes the retiring canonical transcript as data, including original tool calls
and opaque continuation, rather than resuming that native tool exchange.
`contracts/completion.py` supplies immutable per-request options: the summary
output cap and disabled tool selection are identical in audit and transport.
Configured task instructions remain intact for subsequent normal requests.
Pending input cannot be retired. Outstanding calls may cross a compaction
boundary: their original database records remain durable, and turn liveness is
derived from the full scoped transcript, independently of the active model view.
Late results use the canonical inbox and retain their exact correlation; an
original request outside the active view produces neutral incoming evidence,
not an orphaned provider tool result. Compaction neither reissues nor settles
work and does not require copying open request envelopes into the next window.
Oversized consumed history is summarized in complete chunks; every original chunk reaches
the working model and each checkpoint can resume after slice yield. The
recent tail and optional bounded Jev-selected complete groups remain exact.
Jev receives bounded advisory excerpts; its selection never filters the model's
source. Every selector request is audited before dispatch without credentials.
Unavailable selection is a durable notification and falls back to summary plus
recent history. `TYPESAFE_API_KEY` enables this optional adapter.

The summary is committed through the normal outgoing boundary before the scoped
selector is atomically saved. A failed summary or selector write does not advance
the window or consume input. Completed summaries failing validation are retained
only as non-executable invocation audit evidence, with a specific validation
reason and estimated size; they never enter dispatch or replace the active
window. Provider interruptions retain sealed partial evidence through the normal
outgoing boundary.
Compaction never ACKs task input. New arrivals stay
outside the sealed boundary; successful compaction returns to reconciliation
before the next model request. Model-supplied compaction focus is delivered as neutral data, not inserted into
trusted instructions. Fulfilled compaction request IDs are retained in
the canonical summary metadata, preventing a still-pending receipt from causing
repeated compaction across execution slices. Restart selects the same view from
the same log, writer and consumption cursor. Irreducible context fails visibly,
with no silent truncation or automatic capacity retry storm. Interrupted output
also replays in full; the old provider-side failure excerpts are removed.

`tests/thesis/http/test_context_compaction.py` covers full history, manual and
automatic checkpoints, slice-yield recovery, repeated checkpoints/restarts on
memory/filesystem/Cosmos adapters, late arrivals, summary/checkpoint faults,
optional audited Jev selection, and oversized pending input. Pure overlapping
call-group ordering is covered by `tests/thesis_runtime/test_context_window_rules.py`.
The same HTTP suite covers async results during and after real compaction,
restart, duplicate delivery, partial completion and tool-disabled transport
options; the original request records need not remain in the model view.
These mechanical checks do not certify semantic fidelity of generated summaries.

FCA's `extensions/tools/artifacts.py` owns agent-facing artifact windows before
canonical result delivery: 12,000 characters by default, at most 24,000, with
explicit total length, next offset and optional JSON-pointer selection. This
prevents an immutable artifact's complete trade ledger from flooding model
context. The harness artifact contract and deterministic full-file consumers
remain complete. `tests/thesis/http/test_artifact_pages.py` verifies paging,
selection, exact Unicode/binary reconstruction and unchanged full-file access.

## Incremental model output

`AgentSession` delegates provider execution and request auditing to
`sessions/model_invocation.py::ModelInvocation`. The neutral provider port yields
`OutputDelta(reasoning|text)` values and exactly one complete outgoing
`SessionMessage`. Native roles, encrypted reasoning, SSE framing and partial
function arguments remain inside provider adapters. OpenRouter executes one
streaming path; synchronous consumers collect it. Foundry's deployment port
currently supplies a single completed frame rather than live fragments.

Each invocation preallocates `message-<invocation_id>` as its outgoing identity.
Started, bounded delta and interrupted records use the existing per-chat timeline
with `kind=output`, conversation/thread attribution and UTF-8 byte offsets.
Raw output fragments are presentation evidence, never canonical session input,
model history, acknowledgement or permission to dispatch a tool. The first fragment
flushes promptly; a bounded publisher flushes later fragments at about 100 ms or
4,096 characters, including during a provider pause. It inherits the worker's
ownership fence. Generation remains on the current worker thread; cancellation
and steering checks are cooperative and throttled between provider fragments.
Buffered fragments are removed and offsets advanced only after successful
publication, preserving the buffer if a write fails.

New operator input at the same anchor stops generation and closes its stream.
The session seals all received reasoning/text under the reserved outgoing identity
with `result.interrupted=true` and no tool requests. This canonical partial is
durable before the invocation is marked interrupted; it does not ACK any input
or dispatch obsolete calls. The next iteration reconciles durable arrival order,
refreshes tool policy and includes both the partial and new steering. The shared
pure provider projection carries the partial's reasoning as ordinary assistant
evidence, including on replay and providers without a reasoning replay channel.
Post-provider steering before output commit follows the same path.
If the child becomes terminal during that generation, the sealed evidence remains
durable and unconsumed, but dispatch admission prevents another model request or
tool call. Accepted terminal tool completion retains its existing success policy.

The canonical outgoing message is the atomic completion signal for presentation.
It uses the reserved identity and contains display output plus exact, opaque
provider continuation. Display normalization never modifies continuation. Model
invocation success is saved only after that message persists, then the session
acknowledges the included input snapshot and may dispatch tools. Persistence
faults propagate; partial evidence is interrupted when storage is available.
Worker recovery reconciles a committed complete outgoing message as success,
a sealed steering partial as interruption, or interrupts a started invocation
without output. It never replays the provider request or admits stale owner
writes. Terminal invocation updates retain the original audit and creation identity.

Cosmos `fenced_batch` sorts every operation, including ownership/root fences and
caller-supplied root updates, by document identity before executing a transaction.
Admission and progress therefore acquire shared root/counter locks in the same
order. Fence-conflict retry classification tracks indexes after sorting; data CAS
conflicts still return to the caller for recomputation. This prevents the
Postgres emulator deadlock between operator/root message admission and concurrent
root/phase output without weakening ownership or deletion fences.
Each SDK call receives fresh conditional-option dictionaries because the Cosmos
serializer consumes those options. Fence retries therefore retain the original
counter/data CAS, preventing duplicate timeline positions after a root conflict.

`conversations/timeline.py` defines timeline and notification ports. HTTP depends
on the reader port, never the Cosmos class. Cosmos composition supplies one lazy
change-feed watcher per API timeline adapter, using an independent server cursor
and local subscriber fan-out. Active subscriptions poll the shared feed at about
150 ms; no per-chat feed readers or new broker are introduced. Idle watchers
pause and application shutdown closes them. Resubscription captures a fresh server
cursor before reading history, avoiding an idle backlog. HTTP uses chat-scoped
async wake-ups, so unrelated chats do not trigger queries and idle SSE connections
do not occupy blocking worker threads. Local commit hints remain immediate;
the 15-second timeline read is recovery. Full SSE pages drain before waiting.

The UI projects provisional output by message and thread identity and renders it
through the same `ScopeTranscript` and collapsed `ThinkingAccordion` as final
messages. Canonical output suppresses provisional rows, including late history
backfill; expansion survives promotion. Progress updates do not rebuild workflow
indexes. Removed `ReplyChunk`/`ReplyProvider` and the root-only `StreamingReply`
component are not alternative execution or display paths.

Verification: `tests/thesis/http/test_model_output_streaming.py`,
`tests/thesis/http/test_stateless_ownership.py`, the mandatory delivery guard,
`tests/thesis_runtime/adapters/providers/test_streaming.py`, and
`src/ui/e2e/thinking-stream.spec.ts`.

## Status

Normal response completion keeps a turn open while the scoped durable transcript
contains unreturned tool requests or spawned-child handles. Execution ownership
is released until incoming delivery resumes it. No wait tools, blocking child
waits, pending-work store, timer or harness-job monitor is introduced.

Discovery in workflow `6.0.0` develops a Git-committed strategy sketch through
ordinary session tools. Workspace `sketch_contract.py` owns pure contracts;
`sketches.py` owns immutable content-addressed revisions and CAS updates.
Completion carries only `sketchReference`. Model creation, falsification,
verdict and strategy construction resolve that exact revision and its pinned
components. Assumption-to-falsifier assignments and verdict assessments connect
investigations to the sketch. Discovery's prompt and role skill guide a recurring
construction/uncertainty/investigation/interpretation/revision loop. Construction
cards teach causal representation inspection, setup paths and bounded refinement;
operator interjections steer the current question while preserving relevant
evidence and the original attribution of dispatched work. Discovery owns
executable training prototypes, parameter/interaction search and suitable robustness
assessments; diagnostics support this constructive work. Formal falsification and
sealed forward validation remain later phases. A fixed selected-policy null is
conditional evidence unless the full selection procedure actually runs per null.
Sketch.prototype pins the strategy, selected parameters, candidate job and search
evidence. Completion checks bind those facts to the retained observed-data execution;
downstream strategy design preserves this policy and makes selected settings its defaults.
This is agent guidance, not an engine gate or proof of alpha or model compliance.

### Bounded research investigations

`thesis/extensions/workflows/research_investigation/` implements workflow
`research-investigation@6.0.0`. Discovery's skill 1.11.0 prompts default delegation
of substantive implementation/testing while retaining directions, allocation and
integration. Constructions remain provisional, structural alternatives have no fixed
quota, and substantial concentration is justified through evidence. It prompts
concurrent delegation of complementary independent directions, then asynchronous
triage against the current construction and operator input. The hypothesis workflow
retains its existing phases. Each branch has its own execution and workflow node,
parented to the launching discovery conversation, using the unchanged generic host.

| Phase | Executor | Accepted successors |
| --- | --- | --- |
| `proposal` | Model designs/repairs an investigation of the original intent. | `critique`, `focused_critique` |
| `critique` | Structural split, no model; three independent specialist branches. | Specialist critiques |
| `implementation_critique`, `experimental_critique`, `alignment_critique` | Independent model contexts check implementation, experimental validity and goal alignment respectively. | `collect_critiques` |
| `collect_critiques` | Pure deterministic collection preserving each report; no verdict. | `adjudicate` |
| `focused_critique` | Model checks narrow repairs requested by adjudication without restarting all specialists. | `adjudicate` |
| `adjudicate` | Model resolves objections, makes bounded local repairs and assigns the next review; delegates substantial work. | `critique`, `focused_critique`, `proposal`, `investigation`, `completed` |
| `investigation` | Model develops training strategies, searches parameters and checks robustness with targeted diagnostics and artifacts. | `critique`, `focused_critique` |
| `completed`, `failed` | Terminal metadata; no model. | None |

The declared revision/execution caps bound resource use, not scientific decisions.
Critics recognize sound limited studies and adverse findings. The judge may return
uncertainty or an issue requiring discovery's direction. Phase handoffs retain the
original question, constraints, pinned sketch revision and accepted products;
private transcripts never cross. Branches author separate programs and have no
shared-sketch mutation, forward-window or recursive launch capabilities. Strategies
use the existing retained submission/acknowledgement path. executionBudget counts
each batch point, null replicate and technical repair; scientific search breadth
is separately recorded by the model. The same cap bounds investigation entries,
including blocked no-job rounds. Specialist critiques inspect claims before the
repair ledger and require concrete evidence on issues and verified repairs. Issue
IDs are specialist-prefixed; the join collects reports without voting or selecting
a scientific outcome. Existing split/join scopes hide current sibling reports until
collection; the generic host is unchanged. The adjudicator owns reconciliation.
Adjudication's required `review` identifies the next review target, scope and issue
IDs, including after delegated repair; it is null only at completion. Direct
review routes reuse the existing specialist fan-out or focused critic. Adjudicate
has branch author/validate/run tools with the same retained-submission path and
budget. Its immutable repair handoffs retain corrections and evidence across
later adjudications; `reviewHandoff` selects the current product. Prompts direct
independent verification of consequential repairs, reuse of valid evidence and
completion when the local decision is supported. They do not impose a round cap
or change completion wakes. Settled issue IDs require explicit new counterevidence
in `reopenReason` to reopen. An `escalate` resolution removes discovery-owned
decisions from local repair assignments and returns them with the branch outcome.
The runtime snapshots the original operator trigger message independently of the
discovery-authored brief.
Extensions select initial context with explicit `contextMessageIds`; the host
resolves those exact canonical records within the chat, without ancestor traversal.
Each child receives the original text as neutral `agent_context` before its
separate runnable assignment. Source-message/conversation references are provenance,
never privileged roles. Source-keyed delivery is idempotent across interrupted
startup and phase revisits. Handoff zero also exposes `operatorRequest`. Branches may extend or
replace provisional implementations within scope. No new scientific/operator gate
or fixed experimentation checklist is introduced.

Phase seeds carry bounded brief/active-issue excerpts and accepted handoff indices,
not accumulated phase bodies. `read_research_investigations` provides complete accepted-product reads by default
with required `execution_id` and `handoff` selectors. Optional `token_offset` and
`max_tokens` use the fixed cl100k_base read-budget tokenizer (16,384 default,
65,536 maximum). Large products return exact `nextArguments` and a content hash;
changed state is rejected on continuation. Ordinary products are delivered whole,
without JSON section selection or character paging. Handoff zero selects the
original brief; positive indices select immutable accepted products; null selects
current state/branch inspection. The same tokenizer budgets SDK, program,
knowledge and artifact reads through `thesis_runtime/tools/reading.py`; the serving
Rust SDK reference uses tiktoken-rs with matching cursors. This budget includes
JSON string escaping and is distinct from provider context accounting. Container
builds prefetch Python's tokenizer assets for offline runtime reads.
Proposal, investigation and adjudicate agents own author/validate/run tools and durable
submission retention. Proposal reports attempts and evidence before critique;
the phase prompt directs actual initial strategy/study execution, inspecting
completion asynchronously without polling. No engine execution gate is added.
Research executors can read
only their own branch's accepted causal products, never private transcripts.
Proposals record repair responses by issue ID; Critique explicitly records verified
repairs. The accepted issue ledger survives proposal replacement and distinguishes
pending repairs from verified repairs and adjudicated accepted limitations. Scientific
decisions remain model-owned. The current contract rejects prior workflow versions.

Agent prompt deliveries contain their instruction body once, with compact receipt
metadata. Session-facing job status/result tools expose accepted-input references
instead of inline program source. `read_study` and `read_strategy` with job ID reads bounded source ranges
from that job's immutable accepted input, including after a workspace edit; ordinary
workspace reads retain their latest-committed behavior. Full harness job inputs remain
authoritative and available to submission reconciliation.

`extensions/workflows/session_services.py` routes by durable workflow identity.
Discovery alone receives the injected `start_workflow` catalog containing this child
registration, plus `read_research_investigations`; the ordinary root launcher remains
unavailable to other phases. Launch derives parent/attempt identity and the frozen
mandate from trusted context. Existing program tools enforce the inherited development
window and retain immutable job attribution before harness dispatch. An injected
submission observer records the same job ID as exposure in discovery before dispatch.
Discovery provenance accepts delegated evidence only with that exposure and matching
durable parent/child lineage; unrelated executions remain invalid.

Compact branch findings, job IDs and accepted-handoff indices are delivered idempotently
to discovery through canonical
incoming delivery and wake it independently. The host's existing fault outcome is
reused. The scoped read tool exposes accepted products and retained jobs for pending,
failed or uncertain branches, without private transcripts or automatic resubmission.
Version-pinned recovery rebuilds from launch/history and can reconcile completed
delivery. Late returns retain their original brief; discovery's prompt owns relevance
assessment, synthesis, operator steering and the sole authoritative sketch update.

Executable contract coverage is in
`tests/thesis/extensions/workflows/research_investigation/test_workflow.py`: parallel
out-of-order returns, bounded revision, original scope after steering, idempotent
return/recovery, window/scope rejection, pre-dispatch retention/exposure, lost or
mismatched acknowledgements without replay, and delegated discovery handoff. These
tests establish routing/contracts; live model behavior is assessed separately through
`.github/runbooks/thesis-discovery-evaluation.md`.

SDK ABI v8 publishes sequences, events, tables and fitted state. The host
records these and composed indicator histories in immutable job artifact
`exploratory_outputs.json`, retaining undefined reasons and availability times.
Host-owned fitted-state provenance binds the training window and owning job.
`read_study_output` exposes bounded artifact pages without a second evidence
ledger. The SDK tool bag includes causal directional change, rolling moments,
EMA, simple RSI, ATR, VWAP, crossover age, PIP, trailing Haar decomposition and
frozen normalization. Knowledge cards describe timing, weaknesses and runnable
compositions; MODWT/CWT and autoencoders are not advertised as SDK implementations.

Strategies, studies, indicators and screeners accept multiple bar intervals:
`1d` and positive canonical `Nmin`/`Nh`. Replay derives them from one-minute
data with explicit session boundaries. Completed slow frames route before fast
frames sharing availability time; incomplete windows remain missing. Ticks
have no interval. Screeners declare `bar_intervals`, accessing series by
symbol and interval, and retain completed-session as-of-date evaluation.
Later selection, null, variation and forward phase purposes remain unchanged.

Current study authoring uses SDK ABI v8 and shared `MarketConsumer` callbacks.
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
baseline. The registered hypothesis-development topology is version `3.0.0`;
remaining normative recovery and repair-budget requirements below are not a
claim that all of them are implemented.

Workspace program authoring and reference-based workflow completion are
implemented through `workspaces/programs.py` and `thesis/extensions/tools/program.py`.
Model invocations now store reference-only neutral message audits alongside the
actual projected provider request; Cosmos saves that opaque request in immutable
chunks before publishing its invocation manifest. These changes do not establish
complete crash recovery or remote deployment verification.

Argument-schema feedback reports concise paths and expected/received types,
including failures nested in schema unions. Three consecutive tool rounds with
the same structural violations interrupt the turn after persisting their results;
operator input or execution progress resets that slice-local repair budget. The
conversation service durably schedules a backed-off continuation and notifies an
explicit conversational return target without crossing workflow record boundaries;
it does not request human input. The 64-request
execution slice yields into another durable turn with no lifetime iteration cap.
These mechanics do not choose research directions or complete workflow phases. Unexpected root runtime faults are logged and recorded as
interruptions when persistence is available, then propagated so durable workers
cannot mark the failed turn completed.

### Option 1 Session Delivery Contract

Option 1 is the REQUIRED delivery architecture for roots, helpers, and phases.
The neutral delivery modules now exist: `thesis_runtime/contracts/messages.py` defines
`SessionMessage` and `MessageContent`, `thesis_runtime/conversations/inbox.py` owns
`SessionInbox`, `thesis_runtime/conversations/incoming.py` owns the storage writer, and
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

There are two logical message representations: canonical `SessionMessage` and
native `ProviderMessage`. The durable `ChatMessage` is a storage/UI envelope
around canonical content; its body, role and reasoning do not drive replay.
`ModelCompletion` wraps one `ProviderMessage` with invocation attribution and
accounting, rather than defining another copy of message content. Native request
audits and transport use the same `ProviderMessage.as_wire` encoder; execution
does not decode audit dictionaries back into another message model.

Successful model output retains `result.provider_continuation` as an opaque
provider/model origin plus exact replay fields, separately from normalized
`result.reasoning`. The decoder assembles reasoning strings and structured
reasoning blocks, preserving their order, encrypted data, signatures and empty
values. Stream indices are assembly coordinates and are not replayed. DeepSeek
preparation supplies the required empty reasoning field when none was returned.
Only complete outgoing messages supply native continuation; incoming evidence
cannot inject it and interrupted/ambiguous groups do not replay opaque state.
Serving composition binds continuation to its configured provider/model and
rejects mismatches. Existing persistence and chunk hydration carry this data;
there is no second history or continuation store. Old completed model records
with provider attribution but no captured continuation marker are outdated for
resume and rejected. Display text is never used to infer the missing state;
affected development sessions must start fresh at deployment cutover.

Every incoming producer MUST converge on `Conversations.deliver_session_message`
through service or handle delegation, and ultimately
`thesis_runtime/conversations/incoming.py::write_incoming`. `DeliveryService` wrappers use that writer;
standalone delivery also reaches `write_incoming`, not another storage path.
Dispatched tool results use `SessionInbox.deliver` with its injected persistence
port, never native-history appends. A tool outcome containing multiple datums is
ONE incoming receipt with an ordered array; the UI expands that array purely.
Incoming observation does not write another copy. Tool registration owns operation
semantics (`read`, `write`, `execute`, `interact`); the session snapshots that
classification into each canonical result envelope. Harness discovery preserves
MCP read-only annotations, without inferring semantics from tool names or result
content. Live tool chunks and transcript replay project the retained classification;
the UI owns disclosure defaults and rich-result exceptions. Classification grants
no authority and does not change delivery, access policy, or retry behavior.

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
and no invented empty outgoing reply. Mid-turn operator input interrupts generation
cooperatively, seals received reasoning/text without tool requests or ACK, and
starts the next model request with the partial and steering. Other incoming
evidence remains durable for the next eligible model boundary.

Live/replay share `project_native_messages` through pure
`NativeSessionAdapter.request_snapshot`. Explicit correlations join only within
the supplied conversation scope; prose, tool names alone, or foreign history
MUST NOT associate results. Complete protocol groups may defer interleaved
evidence in projection, never rewrite the log. Partial/ambiguous groups preserve
evidence without fabricated calls/results. The adapter owns no persistence,
scheduling, ACK, dispatch, runtime callbacks, or service introspection; only its
stream wrapper calls injected `provider.stream`; synchronous consumers collect that same stream.

Provider snapshot audit is REQUIRED, not optional. Before provider execution,
the session through `ModelInvocation` MUST persist a `STARTED` `AgentInvocation`
with reference-only `request_messages`, input hash, and detached opaque
`provider_request`. Ordered references contain only `message_id`, `direction`,
and `position` from the exact working snapshot; canonical bodies, tool arguments,
and results remain in the neutral transcript, not a second full canonical audit.
Historical full-message invocation audit formats must be rejected under the
current-contract policy; their acceptance is removed.
The opaque audit
records actual projected messages, advertised tool catalog,
options, protocol-mapping/projection version (`native-session-v6`), and
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

Record-only workflow anchors retain incoming phase results without runnable
work. Conversation delivery applies this routing decision before atomic message/work
admission, and turn execution ignores record-only anchors even if stale work exists.
The durable worker therefore cannot run a model at the workflow bookkeeping node
or complete it before the workflow reaches its terminal outcome. The durable
research-loop behavior test in
`tests/thesis/extensions/workflows/research_investigation/test_workflow.py` verifies
all active phase reads/completions, record-only delivery, and stale queued wakes.

Root, helper, phase, and model-only turns still execute `AgentSession` through
`Conversations.run_node_turn`; model-only means an empty registry, not another
executor/dispatcher/scheduler. Production scheduling uses durable work claims. Steering uses the canonical message factory with
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

The product composition injects the workspace catalog's durable deletion read
port into `ChatService.bind_workspace_deletion`. A marked workspace fences runtime
record reads, writes and intake, including work admitted before deletion.
`delete_workspace_conversations` marks every persisted root (including archived
chats) through existing conversation deletion and resumes bounded cleanup. The
HTTP application repeats this reconciliation at startup for pending workspace
markers. This lifecycle fence introduces no incoming writer or provider path;
the full storage deletion scope and deferred non-conversation erasure are defined
in `fca-thesis.md`.

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

### Automatic provider recovery

`sessions/recovery.py` supplies three cooperative backoff delays (1, 2, 4 seconds)
for `AgentSession`. Transport interruptions, premature SSE EOF, HTTP 408/429/5xx
and explicitly transient upstream stream failures can continue automatically.
The parser retains numeric SSE error codes and actual finish reasons. Empty or
truncated completions retain usage/generation metadata and receive short recovery.
400/401/402/403 and malformed output/local protocol faults bypass short retries
and yield to backed-off continuation with supervisory notification. OpenRouter's
[documented error contract](https://github.com/OpenRouterTeam/docs/blob/main/api_reference/errors-and-debugging.mdx)
can report failures inside a successful HTTP stream. Numeric/date Retry-After is
honored up to 60 seconds within a slice; longer hints are retained as the next
continuation resume time rather than retried early.
Successful model requests and new operator input reset consecutive-failure count.

Each failed model invocation retains its original audit and FAILED state. Received
reasoning/text is sealed as canonical outgoing partial evidence with
`result.interrupted=true` and `result.failure`, without executable tool requests.
No empty outgoing message is invented when nothing was received. Large failed
outputs remain complete canonically but project labeled bounded excerpts with
message references, so reasoning-only failures cannot poison the next context.
The current audit projection is native-session-v6. The shared pure
provider projection distinguishes provider interruption from operator steering.
Recovery notices enter the singular incoming writer as neutral runtime evidence,
never fabricated operator input. A direct helper may notify its conversational
return target. A phase returning to a workflow record retains its failure and
continuation locally; recovery never skips that record to notify or wake an
ancestor conversation. Explicit workflow outcomes keep their declared return path.
Every recovery is a separately audited fresh model
request in the same session/agent/intent epoch, using retained tool receipts and
job references. Failed requests ACK no pending inputs; only successfully committed
output enables normal ACK and dispatch. A failure to persist the failed audit,
partial output or recovery notice propagates and cannot start another model request.

Backoff checks cancellation, steering and ownership; lost ownership escapes without
further dispatch. Retry exhaustion returns the existing interruption outcome and
durable canonical continuation, not a needs-input state or workflow failure.
The conversation service preserves a resume time and failure count, yields to
steering/cancellation and schedules through the existing intake/work path.
Explicit agent approval requests still pause; later hypothesis-phase approval
protocols are unchanged. Only discovery and its research children drop routine approval. A process/worker crash is still an unknown
execution: existing fenced recovery records interruption without automatic replay.
Subsequent authorized execution rehydrates the same canonical evidence and jobs.
No retry queue, alternative inbox or tool/submission replay path is introduced.

Behavior coverage is in `tests/thesis_runtime/test_provider_recovery.py` (actual
OpenRouter HTTP exchange through durable root/phase conversations), the session
audit/delivery suites, and the durable study-submission recovery case in
`tests/thesis/extensions/workflows/research_investigation/test_workflow.py`.
`tests/thesis/http/test_recovery_scope.py` verifies workflow containment, direct
helper notices, scoped durable wakes and recovery after worker replacement.

Discovery declares windows first, commits a sketch and delegates substantive
construction/testing. Its concise assignment retrieves SDK guidance by need.
Implementation phases share `prompts/sdk.py`; specialist review prompts describe
review duties rather than candidate development. Operator acceptance is stated
once per conversational phase and remains agent-owned. No prompt-size quota,
review checklist engine or runtime repair gate is introduced.

The changed hypothesis-development topology is version `3.0.0`. Definition
requests can name a version; unsupported historical versions return an explicit
diagnostic rather than substituting today's legal edges. Persisted retired phase
values must be rejected, not mapped into the current topology. Necessary data
conversion belongs to a one-time cutover outside the workflow engine.

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

The durable sub-agent runtime is implemented in `thesis_runtime`:

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
  completion and operator input to the conversation handles;
  Local and deployed serving use this same implementation. Entrypoints must not
  subclass it for logging or replace its completion methods: trusted phase-attempt
  identity must reach the shared completion authority unchanged. The local wiring
  boundary is guarded by `test_local_entrypoint_uses_shared_control_service_without_method_overrides`;
  the real tool/service attempt contract is exercised in `test_workflow_graph.py`.
- `DeliveryService` adapts child results, workflow outcomes, and operator answers
  to the canonical incoming writer, then routes the anchor's wake, durable before
  routed and idempotent under retry, with `SubAgentNotifier` folded into it;
- `AgentSession` is the single durable model/tool turn loop that the chat path
  and workflow path use through `Conversations.run_node_turn`: it sends the merged catalog, persists each
  model and tool invocation as a first-class record, threads tool results back
  as neutral incoming messages, stops on a terminal control tool, and yields bounded execution slices
  without limiting the agent lifetime;
- `messages.decode_transcript` is the shared neutral durable-history decoder,
  scoped by anchor: the root uses only root messages and a child only its own.
  `conversations/output.py::persist_turn` writes outgoing envelopes only; incoming
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
REQ-034..REQ-040) is implemented in `thesis_runtime.conversations.conversation` and
`thesis_runtime.subagents.child`:

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
  Zero-position historical intake is removed. Persisted incoming messages must carry their admitted
  position; an unpositioned transient draft is not a historical durable message.
  Persistence I/O does not hold the global worker-map or intake guard;
- child `send` routes through anchored answer admission, checking scope,
  lifecycle, and operator policy before persistence and wake. It cannot bypass
  admission by writing directly to the shared chat;
- `ChildConversation` composes the return layer over an inner conversation:
  the `returns_to` anchor, `complete`/`fail`/`result`/
  `children`/`cancel`, and one turn-end policy. Durable `CompletionMode` selects
  `OneShotPolicy` (`complete_on_text`) or `ConversationalPolicy`
  (`explicit_completion`), independently of the input `kind`.
  `returns_to_sub_agent_id` stores the return anchor independently of parentage,
  and `phase_executor` is explicit completion authority. Phase executors always
  use explicit completion, even with one-shot input. The codec does not infer these fields. The current contract requires explicit
  persisted fields and rejection when required metadata is missing;
- the conversation-facing workflow host sees the service (`ports.conversations`) and the
  handles: phase children are spawned through `service.spawn` with the
  workflow node as their return anchor, phase results route through the
  service's delivery, and the engine builds no spawner, delivery, or scheduler
  of its own;
- phase entry is the sole startup path for phase agents. Accepting a transition
  closes the source and enqueues the successor without creating an unprompted
  agent. Entry reuses the phase's existing conversation on revisits and delivers
  the attempt-scoped prompt through `Conversations.spawn`;
- phase specs are the sealed union of frozen dataclasses:
  `ConversationPhase`, `OneShotPhase`, `DeterministicPhase`, and `TerminalPhase`.
  These are host activity bindings; the generic graph reads only named inputs,
  selected routes and terminal outcomes.
  Exhaustive dispatch keeps invalid combinations unrepresentable. Deterministic
  phases create no conversation; terminal phases have no executor or contract.

`thesis_runtime/contracts/activity.py` owns the immutable discriminated `ExternalInput`,
`ModelOutput`, `RuntimeTool`, and `Delivery` contracts, their generated schemas,
and the canonical activity decoder. Transcript seeding, anchor ownership,
input recognition, and persistence consume that contract instead of independent
metadata heuristics. Every activity carries `activityKind`; a record without
canonical session data is not decoded.

Stream brokers are process-local hints. Production execution ownership and
mutation fencing use the selected shared catalog and independent durable workers.

The FCA workflow lives in
`thesis/extensions/workflows/hypothesis_development/`. Its endpoint owns native
domain types, phase implementations, reducers, validation, reports, and
composition services. The generic engine communicates with that endpoint using
the detached JSON protocol described above. Retired sibling workflows remain
removed.

`thesis/extensions/workflows/bindings.py` defines FCA `WorkflowSessionServices`,
`WorkflowSessionBindings`, and `RuntimeToolActivity`. The concrete
`session_services.py` owns role policy, knowledge restrictions, authoring, and
completion validation/application. Spec-only services restrict retrieval and
advertise schemas when execution is unavailable; they cannot accept completion.
Runtime program-reference checks use the canonical conversation activity writer.

### Committed Workspace Programs

Application composition supplies `WorkspacePrograms` with an `AuthoringSdk`
capability independently of its execution client. The SDK implementation in
`workspaces/sdk/` owns source identity, metadata agreement, token reference
validation and template rendering. It receives only a metadata/source port and
local SDK root, never the program lifecycle service or its repository/submission
state. `workspaces/program_contracts.py` owns their shared value and failure
rules. Dependency guards in `tests/thesis/workspaces/test_sdk_architecture.py`
protect this direction; public program behavior remains covered through serving
composition and program operations.

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

`thesis/extensions/tools/program.py` binds these operations to trusted `ToolContext`
workspace scope and the existing registry/dispatcher/inbox. Author/update alone
accept source or patches; validation/run accept only `program_id`; list tools
enumerate committed IDs with an optional prefix and bound. Program bindings
replace conflicting raw harness and legacy author registrations. Roots receive
the composed service; workflow bindings grant it only to the owning discovery,
model-creation, strategy-design, or strategy-backtesting executor, not helpers
merely inheriting tool names. The effective tool policy still filters the catalog.

Agent execution calls return admission receipts immediately. The immutable
submission retains its trusted conversation recipient before dispatch. Terminal
publication in the harness commits a compact completion outbox event in the same
conditional write as the result. Finite workers POST that event to the configured
Thesis completion endpoint; the existing control reconciler retries unacknowledged
delivery events. No caller or background job monitor polls execution status.
The receiver resolves the retained job ID and delivers once through
`Conversations.deliver_session_message`. Failure/cancellation is terminal evidence;
stopped, cancelled or stale-epoch recipients retain late results without revival.
A stable message identity makes lost HTTP acknowledgements safe to redeliver.

Liveness is derived from exact request correlations and runtime-authored
`pendingJobs`/`completedJobs` fields across the full scoped transcript. A one-shot
agent ending its response remains waiting until its results arrive, including
across compaction and when a completion precedes its admission receipt. A normal
response can end while other work runs; new incoming evidence resumes the agent.
Read-only failures remain safe observations with actionable diagnostics;
unknown-outcome mutation recovery retains the original IDs without resubmission.

`read_sdk_reference` supplies bounded source pages from matching SDK sources,
the `decimal` kernel topic, and study/strategy/indicator templates. `template=basic` renders declared
subscriptions into the matching template. Version and ABI metadata come from
the SDK's `Cargo.toml` and `src/abi.rs` and must match the canonical packaged SDK.
Reads report selected `startLine`/`endLine`, `totalTokens`, `tokenizer`, and
`nextArguments` with original-range token offsets. Whole topics fit the default
16,384-token budget; large topics continue within the same selected source range.
There is no hidden line-count or character page cap. Rust serving and Python
local/template projections share this current contract, with exact UTF-8 pages.
SDK identity hashes Cargo manifests and Rust source files across `strategy-sdk`,
`contracts`, and `num`; dependency changes invalidate stored validation. The wheel
and source distribution carry these canonical crates with portable relative Cargo
dependencies, not a separate Python-side SDK copy. Remote harness access does not
supply local references. Missing or mismatched sources fail explicitly. The
study/strategy/indicator templates in `workspaces/sdk/templates.py` are shared by reference reads
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

Concrete `spec.phase_declarations()` remains authoritative for hypothesis phase
policies, schemas and domain pipes. Its serial domain fold remains
`rebuild.fold_transition`. The generic host now validates and reconstructs control
through pure `workflows/graph/topology.py` and `graph/control.py`; extensions receive
each active position's causal history through JSON protocol 2. Domain folds do not
select the host's active nodes.

Named routes wire output to explicit node.input destinations. Multiple selected
consumers create concurrency; separate required input ports provide synchronization.
Alternative conditional producers feed one port. Forward branches and loopbacks
share route semantics. The pure graph engine has no agent or conversation imports;
the conversation-facing host binds activity implementations and applies effects.
Research critique is a route to three reviewer inputs, not a structural phase.
The collector receives named accepted reports as ordinary activity inputs.

Acceptance verifies intent epoch, executor/chat/parent/node and exact attempt.
Under the shared execution guard it commits history by revision CAS, projects
`active_phases` and `control_sequence`, and closes the accepted executor. Duplicate
identical acceptance returns its journal receipt; conflicting content is rejected.
Events, delivery, extension work and successor dispatch stay outside the guard.
Durable work is per attempt. Recovery preserves arrivals, repairs missing frontier
publication/dispatch and accepted terminal finalization, and may recompute only
explicitly replay-safe deterministic activities. Execution lifecycle fences unfinished conversations.
No additional scheduler, transaction framework or materialized graph store exists.
See [the route and input contract](thesis-fixed-workflow-graph-plan.md) for supported
topologies, cutover and the bounded behavior verification scope.

The HTTP factory registers root and `/api` routes on the same app; both resolve
the same app-state services and authentication. Defaults are fresh per factory,
composed once, and never created lazily by requests. Production explicitly uses
`in_memory=False`; absent services return typed 503 errors. `http/schema.py`
exports the app's OpenAPI 3.1 plus runtime activity/message/datum/tool schemas;
`http/wire.py` derives durable record fields from their dataclasses and response
envelopes from typed models, including complete input receipts, display messages
and flat node summaries. Bounded history previews and canonical item detail reads
share IDs/sequences with the single SSE stream; they never enter provider context,
inbox persistence or consumption. Storage receipt-header queries select caller-owned
result types and metadata fields before paging. PostgreSQL and Cosmos project
headers directly, with explicit hydration for current chunk manifests. No second
stored log or writer is introduced.
Schema generation and wire validation are implemented. This does not establish
complete side-effect recovery or output-publication receipts for every operation.

Local Thesis selects `THESIS_PERSISTENCE=postgres` with a native psycopg pool;
Azure Thesis retains its configured Cosmos adapter. `adapters/persistence.py`
selects the backend explicitly. PostgreSQL stores current JSON payloads inline,
including exact opaque provider request audits, in indexed JSONB documents.
Generated operational columns index scopes, sequences, inbox positions, queued
work, ownership resources and execution identities. One short workspace advisory
transaction validates every enclosing owner and the root deletion fence, then
commits the counter, canonical message, root bookkeeping, inbox head and runnable
reference atomically. Workflow updates retain revision CAS. Model/network work
never runs in that transaction. LISTEN/NOTIFY is a commit hint; durable reads and
reconciliation remain authoritative after disconnects. FCA investigations and
submissions use separate collection scopes on the same selected pool.

The worker runs notification discovery, retained-work reconciliation and workflow
repair independently of its eight execution slots. Each background operation has
at most one call in flight. Retained-work reads return at most 64 references;
payloads load only after ownership. Keyset identity pages and eight-execution
repair passes avoid startup-wide payload hydration. Completed sweeps wait a minute;
full batches continue promptly. Storage failures back off without blocking live
notification dispatch. Running attempts whose owners expired are interrupted and
reconciled, never replayed. Necessary local cutover is a bounded offline operation,
verifying exact hydrated content/IDs/cursors/audits, with no converter in runtime.

Cosmos commits a message or event by writing the per-chat sequence counter CAS
and the item create in one transactional batch in the workspace partition.
Messages, events, and immutable first-save execution snapshots are the same
documents the timeline reads, so there is no catalog-to-timeline projection,
deferred repair, or second container. A failed batch consumes no sequence;
concurrent writers retry conflicts. Timeline readers snapshot the in-process
commit version before reading durable items and wait for a version change;
the database sequence remains the only cursor, and a recovery interval bounds a
lost wake. `open_cosmos_chat_stack` and `open_execution_store` call
`ensure_database`, which creates the database and catalog container
if absent. Opening these stacks is not side-effect-free.

The harness `run_study` tool is implemented across the Rust workspace and the
harness-hosted MCP endpoint:

- `run_study` is a capability-gated MCP tool with a `StudySubmitter` boundary,
  a remote `POST /v1/studies/jobs` route, and a `harness study-job` entrypoint
  that writes a `study.json` artifact with the `study` shell;
- the study execution mode is Rust-JIT: the authored source compiles through
  `harness-strategy-jit` against `harness-strategy-sdk` ABI v8 and implements
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

The public `Conversations` service remains in `conversations/conversation.py`.
Its implementation separates the anchored `Conversation` handle in `handle.py`,
one attempt's lifecycle and input state in `execution.py`, outgoing persistence
and runtime activity in `output.py`, and continuation policy in `recovery.py`.
`incoming.py` owns the sole incoming write and detached presentation preparation;
`admission.py` owns shared ordering rules. Composition injects the existing writer
and callbacks into one concrete attempt object. No new scheduler, inbox, service
interface, or persisted contract accompanies this internal split. Upstream callers
continue to use the service and handles rather than turn machinery.

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
| Required modules fail when absent; AST imports/calls and aliases reject native session construction, hidden incoming writers, parallel result queues, and adapter effects; reference-only audits, unconditional pre-provider persistence, terminal preservation, exact root reference/globs, and mutation controls remain mandatory with no optional bypass | `tests/thesis_runtime/test_delivery_architecture.py` |
| Every source (operator, initial prompt, tool result, helper result, workflow outcome, runtime notification/check, repair, asynchronous completion) converges on the writer; receipt identity, arrival order, assigned input position, retries, visibility-before-wake prevention, and one receipt for multi-datum outcomes | `tests/thesis_runtime/test_canonical_delivery.py`, `test_initial_intake.py`, `test_session_delivery.py`, `test_session_live_activity.py` in the same directory |
| Canonical decode and live/replay equivalence, exact scoped correlation, only runtime-configured initial bootstrap projects as native system, a record without canonical session data is not decoded, foreign/reused IDs, partial groups, and interleaved inputs without fabrication or privilege | `tests/thesis_runtime/adapters/providers/test_session_adapter.py`, `tests/thesis_runtime/test_session_delivery.py` |
| DeepSeek tool exchange preserves prose under whole-string regex generation, canonical argument validation and live/replay history, records family/version and upstream identity, and leaves other families' native schemas unchanged | `tests/thesis_runtime/adapters/providers/test_model_families.py` |
| Repeated structural argument failures interrupt without dispatch; operator steering resets the repair budget; unexpected root faults produce durable notices and interrupted worker attempts without replay | `tests/thesis/agent/test_root_live_persistence.py`, `tests/thesis/http/test_stateless_ownership.py` |
| Mandatory opaque audit retains actual protocol mapping, advertised tool catalog and their versions/options, ordered canonical message references and request IDs without full canonical duplication; detached, hashed caller persistence and Cosmos request chunks precede execution and survive success/failure terminal updates; adapter stays pure and audit/output persistence faults prevent execution/ACK/dispatch as applicable | `tests/thesis_runtime/test_session_input_snapshot.py`, `tests/thesis_runtime/adapters/providers/test_session_adapter.py`, `tests/thesis_runtime/adapters/cosmos/test_chunks.py` |
| Live inbox reconciles scoped authoritative durable order at each eligible model boundary; arrivals after provider pull and before output persistence never rearrange the live snapshot | `tests/thesis_runtime/test_arrival_reconciliation.py`, `tests/thesis_runtime/test_session_input_snapshot.py` |
| Only pending inputs actually included in a successful provider request are ACKed after output persistence; empty/unusable completion interrupts without ACK or invented empty reply; initial pending input, mid-model and post-provider arrivals, interrupted/provider/output/ACK faults, partial activity, and results after terminal requests stay truthful and unconsumed until eligible | `tests/thesis_runtime/test_session_delivery.py`, `test_canonical_delivery.py`, `test_session_input_snapshot.py` in the same directory; `tests/thesis_runtime/test_arrival_reconciliation.py` |
| Operator steering closes the active stream, seals received reasoning/text without tool dispatch or ACK, and starts a new request whose live/replay context contains the partial and steering; phase execution remains running. Recovery retains sealed partials as interruption without replay | `tests/thesis/http/test_model_output_streaming.py`, `tests/thesis/http/test_stateless_ownership.py`, `tests/thesis_runtime/test_arrival_reconciliation.py` |
| Concurrent operator/root admission and root/phase progress acquire shared Cosmos document locks in the same order while retaining CAS, ownership, and deletion fences | `tests/thesis_runtime/adapters/cosmos/test_conversation_scope.py`, `tests/thesis/http/test_stateless_ownership.py` |
| Asynchronous job completion resumes parked responses; terminal returns deduplicate and retain late evidence without revival | `tests/thesis/http/test_job_completions.py`, `tests/thesis/extensions/workflows/research_investigation/test_workflow.py`; harness `execution-storage/tests/job_outcomes.rs`, `api/tests/job_completions.rs` |
| Synchronous and asynchronous completion, skipped/not-started calls, unknown mutation outcomes, terminal/cancelled late delivery, and private child transcripts do not resurrect nodes or replay effects | `tests/thesis_runtime/test_canonical_delivery.py`, `test_session_live_activity.py`, `test_durable_architecture.py` in the same directory; `tests/thesis/extensions/workflows/hypothesis_development/test_live_tool_recovery.py` |
| Restart/reload/resume preserve all incoming sources and per-anchor input/consumed positions, pending partial groups, correlations, idempotency, delayed-wake fences, and the same projection, without synthetic results or side-effect replay | `tests/thesis_runtime/test_durable_architecture.py`, `test_turn_retirement.py`, `test_unified_conversation.py` in the same directory |
| Steering epoch is installed before canonical wake for synchronous and asynchronous workers; root/model-only routing, source completion, node lifecycle, store CAS, and effects outside the acceptance guard remain unchanged; deterministic executor faults still fail workflows | `tests/thesis/workflows/test_steering_delivery.py`, `test_unified_session.py` in the same directory; `tests/thesis/http/test_input_acknowledgement_wiring.py`, `test_serving_midturn.py` in the same directory; `tests/thesis/extensions/workflows/hypothesis_development/test_acceptance_fence.py`; `tests/thesis_runtime/test_lifecycle_acceptance_guard.py` |
| Exact reasoning and structured continuation survive rejected tools, multiple tool turns, Cosmos hydration and replay; UI normalization is separate, incoming data cannot inject state, and incompatible provider/model replay is rejected | `tests/thesis/http/test_provider_continuation.py` |

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

Hypothesis-development exposes the phase's permitted program, sketch and SDK
tools from the first model request. Research-window declaration does not alter
the advertised catalog. Discovery's prompt directs it to call
`set_research_window` first. Workflow bindings inject `resolve_data_window`;
program, sketch and exploratory-output handlers check that prerequisite at call
time. Missing windows return a model-visible `research_window` / `not_started`
failure naming `set_research_window` and instructing the model to declare first
and retry. Research-launch and completion errors also name that repair action.
Declaring a window and authoring within one tool-call bundle uses the newly
persisted declaration. Study, backtest, parameter-batch and null-batch execution
pass the resolved policy to the program service before job allocation, retention
or dispatch. Development and sealed-forward restrictions remain enforced there.
Chat-level tools may deliberately have no resolver. Phase authorization still
comes from trusted bindings and the immutable allowed-tool intersection.

Generic child completion guidance follows the durable `completion_mode`:
explicit completion requires its advertised tool, while a text answer only ends
a turn. The runtime does not choose research branches or automatically retry a
phase that ends a turn without pending work.

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

All sub-agent creation MUST pass through the `spawn_sub_agent` command handler.
A workflow deterministically starting a phase MUST invoke that same command
contract as a first-class tool invocation with workflow actor scope and MUST
persist its returned sub-agent handle.
A model dynamically creating a helper invokes it as a normal tool call.

`spawn_sub_agent` is asynchronous. Its immediate result MUST return the durable
child id and initial state to the caller. At a normal model stop, the session
checks exact ToolCorrelation joins for unreturned calls and existing child-handle
receipts for missing child-result messages. Pending work returns `waiting` rather
than automatically completing a one-shot task or scheduling another model call.
The full scoped transcript is checked, including evidence before compaction;
only the active context is sent to the model. Incoming input already admitted
during the response is processed before completion. Late arrivals wake the
existing serialized worker. No wait tools, separate pending-work store, timers
or submission replay are introduced.
Child output MUST NOT be injected into the parent conversation implicitly.
The child requests terminal completion through `request_sub_agent_completion`;
the result is constructed against the completion contract captured in its
specification. Canonical delivery supplies that result to the parent.

Each child MUST inherit workspace and chat scope. A child MUST receive a budget
no greater than its parent's remaining delegated budget. Spawn authorization,
maximum depth, and maximum active children MUST be enforced deterministically.
Spawned sub-agents MUST be `conversation` or `one_shot`; deterministic work is
not represented as a sub-agent.

`request_sub_agent_completion` MUST use the same typed construction feedback
and repair-turn machinery as phase completion. Its deterministic lifecycle
validator replaces the workflow acceptance gate. Acceptance marks the child
completed and freezes its terminal result for canonical delivery to the caller.

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

### Bounded execution slices and autonomous repair

The session bounds each execution slice, not the lifetime of an agent. Reaching
64 iterations returns yielded and the conversation service persists a runnable
continuation through the canonical incoming writer. Three identical schema-failure
rounds and exhausted provider/catalog recovery also retain continuation, with
cooperative increasing backoff and a supervisor notification. Pending inputs are
not ACKed by failures, delays or scheduling. Actual completed tool calls and
unknown-outcome submissions are never automatically replayed.

Source/compiler and research repairs remain agent decisions. Discovery delegates
most implementation/testing to research branches and redirects scoped blockers.
Working implementations can return through the existing review loop before large
searches; a plan review does not verify later code. Runtime recovery does not
choose scientific outcomes. Discovery progresses autonomously, while later phases
retain their mandatory conversational approval protocols. Explicit cancellation,
terminal state, ownership and persistence fences remain authoritative.

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
`harness-strategy-jit` against `harness-strategy-sdk` ABI v8. It implements
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
   After construction, ABI v8 exposes the canonical source-and-dependency union
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
   MUST rebuild for ABI v8.
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
   trigger undeclared trade fetching. Backtests always add quote/trade execution
   feeds for every input symbol. The manifest distinguishes
   `effective_subscriptions`, `execution_subscriptions`, and their normalized
   `acquired_subscriptions` source union. Execution feeds do not expand the
   consumer echo or callbacks; `simple_touch` fills from ticks, never bars.
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
counts. SDK ABI v8 invalidates older compiled artifacts; authored studies and
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
training strategy/study execution and inspection tools. Discovery, model creation, and strategy
design share author/update/read/validate/list tools for studies, strategies, and
indicators, plus `read_sdk_reference`; only discovery adds `run_study`.
Discovery also receives `run_backtest`, `run_backtest_batch` and `run_null_batch`
under the development-only mandate. Research investigators receive the same
training execution tools; reviewers receive source/evidence readers only. The shared SDK authoring guidance is a version-matched authoring
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
before any submission. Agent-facing calls return retained IDs immediately; terminal
evidence arrives through asynchronous canonical completion delivery for each point.
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

Workflow-scoped data windows come from discovery's explicit research mandate
persisted in the execution result, bounded by the launch timestamp. Its forward
window remains sealed, and every program run is checked against the
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

In the current `3.0.0` topology this contract is returned by the deterministic
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
sub-agent conversation from current-contract durable turns, restore a
current-contract checkpoint, and resume only unfinished invocations. Outdated
formats or workflow versions MUST be rejected, not upgraded, interpreted through
historical decoders, or resumed using a different workflow definition.

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
transitions between them, extension handler names for per-phase seeding, completion validation and
deterministic execution. Native piping and workflow-start handoffs belong to
the concrete extension behind its JSON endpoint. The general implementation
(spec, builder, registry, machine, delivery plumbing, and handoff and completion
application) MUST live under `thesis_runtime/workflows/` and MUST contain no
workflow-specific behavior or contract. A phase agent owns its turns and
advances the workflow only by calling the completion tool; a deterministic phase
may declare an executor instead of an agent seed.

- Rationale: one engine, many workflows; the hypothesis workflow is a
  configuration of the engine, not the engine itself.
- Verification: a second workflow can be registered and executed with no edits to
  `thesis_runtime/workflows/`.

**REQ-027: Builder and registry.** A `WorkflowBuilder` MUST assemble a spec from
its parts and validate it (a phase declares a seed or an executor, `on_complete`
names a declared transition target, every transition is in the table, terminal
phases exist). A `WorkflowRegistry` MUST retain one current registration per
workflow ID. New launches use that registration; recovery MUST require its
version to equal the durable version before dispatch. The registry MUST NOT
retain historical implementations or substitute the current spec on mismatch. The
workflow tool MUST dispatch by registry lookup rather than a named
per-workflow method. `execute` MUST be the single generic entry point.

- Rationale: construction and dispatch are data, not code branches.
- Verification: an unknown ID, duplicate registration for an ID, or outdated
  durable version is rejected; adding a workflow requires only registration.

**REQ-028: Handoffs are explicit and transcripts are private.** Cross-phase data
movement MUST flow only through the accepted completion contract and the declared
piping adapters; an agent MUST NOT read another agent's transcript. The
workflow-start adapter threads the caller's message into the first phase.
Continuation MUST rely on durable accepted history, launch input, the scoped
transcript and referenced artifacts. A version-pinned recovery binding repairs
missing dispatch without replaying completed or interrupted effects. Native
domain state is folded inside the extension, not decoded by the host.

- Rationale: a phase's inputs are explicit in its seed, and durability is the
  conversation itself.
- Verification: a fresh workflow object reseeds the next phase from the accepted
  contract and referenced artifacts alone.

**REQ-029: Concrete workflows are self-contained packages.** Each concrete
workflow MUST live under `thesis/extensions/workflows/<workflow-id>/` with its phases,
contracts, prompts, seeds and phase modules, and MUST NOT be imported by
another workflow.

- Rationale: partitioning makes workflows independently replaceable and keeps
  `thesis_runtime/workflows/` stable.
- Verification: the hypothesis workflow imports nothing from
  `thesis/extensions/workflows/**` other than its own package.

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

**REQ-033: Transcript-derived asynchronous continuation.** A child's
terminal result MUST be delivered at its caller's anchor and schedule the
caller's turn, except for workflow record anchors which never run. A normal
model response MUST leave the logical turn open while exact request/result
correlations or spawned-child handle/result messages show outstanding work.
The worker MUST release ownership until new canonical input arrives. There
MUST NOT be a model wait tool, polling wait, second inbox or pending-work store.
Success, failure and cancellation are delivered outcomes; a rejected call with
a failure receipt is settled. Automatic one-shot completion requires all
returns to be present and consumed. Explicit completion/cancellation retain
their existing lifecycle authority.

- Rationale: requests, handles and results already exist durably; derive
  liveness from those facts and use existing incoming wake routing.
- Verification: `tests/thesis/http/test_async_returns.py` covers exact joins,
  worker replacement, partial child returns, one-shot task retention,
  success/failure/cancellation, rejected spawns and racing local input.

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
target, `complete`/`fail`/`result`/`children`, cancellation,
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
Required durable fields MUST be explicit. Persistence decoding MUST reject
missing required metadata rather than infer it from historical fields. The codec enforces this requirement.

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
`thesis_runtime.conversations.conversation` and `thesis_runtime.subagents.child` (one
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

The runtime and FCA package tree above is authoritative. Each source directory
has a local README with one entry per code file. Generic facade exports stay
within `thesis_runtime`; FCA workflow exports live in its extension facade.
Concrete workflows register their own ID, version, launch schema, endpoint and
recovery binding with `thesis_runtime/workflows/registry.py`. The registry holds
one current implementation per ID; duplicate registrations and durable-version
mismatches fail. Durable codecs require every serialized field, including explicit
ownership, completion/return authority, cursor maps, and invocation snapshots.
Invocation audits contain exact message references; historical full-message
snapshots and untagged activity are rejected. Accepted inputs require positive
durable positions; no zero-position intake branch or historical pending map remains.
Provider invocation uses one explicit response-format contract without signature
inspection or an optional provider facade. Generic host code
must not import FCA or a concrete workflow. The native FCA reducer is shared by
the extension's live projection and replay, behind its JSON endpoint.

HTTP schema export remains `thesis/http/schema.py` and `wire.py`. The timeline
transport remains in `thesis/http/timeline.py`; runtime streaming primitives are
in `thesis_runtime/conversations/streaming.py`. Workspace publication, SDK assets,
hypotheses, investigations, and research evidence remain FCA responsibilities.

## Migration Plan

These waves remain subject to the current-contract policy in
`repository.md#current-contracts-and-data-cutovers`: update affected callers
together, remove superseded interfaces, and decide the scoped data cutover.
They do not authorize compatibility facades or ongoing historical decoders.

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

- Implement `spawn_sub_agent`, `request_sub_agent_completion`, and
  `request_operator_input`.
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
- Update all callers to the current public interface in the same change; remove
  the superseded facade. Verify the current operator behavior and recovery
  contract without retaining old APIs or formats.
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
| CON-006 | A conversation accepts mid-turn input; active request snapshots are immutable. | Operator steering closes generation and seals partial reasoning/text before the next request; durable input is consumed only after successful output. |
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


### Stateless API and durable execution ownership

HTTP replicas accept canonical input and read durable state. Production composes
`thesis_runtime/execution/worker.py` with `thesis_runtime/adapters/cosmos/work.py` and `thesis_runtime/adapters/cosmos/ownership.py`; the
`thesis worker` entrypoint executes work independently of `thesis serve`.
API replicas may scale to zero. The worker Container App retains one warm replica
and permits multiple replicas; correctness does not depend on replica count.
Local `thesis_local_api.py` embeds the same durable worker for convenience.

Canonical incoming persistence remains `thesis_runtime/conversations/incoming.py::write_incoming`.
The catalog transaction creates the canonical message, timeline sequence,
conversation inbox head, updated chat links, and runnable reference together.
Admission performs no history query and scheduling never creates that reference
again. Cosmos input positions reuse the chat-wide timeline sequence: they are
strictly increasing within each conversation, with gaps from outputs, events,
and sibling conversations. `arrivalSequence` is storage-owned envelope metadata
which orders admitted records independently of host timestamps. Neutral session
output positions remain zero. Ordinary API admission reads the sequence counter
and chat root, then commits one batch conditional on both revisions. It takes no
chat/intake lease and returns the committed record without a final read.
Consumption acknowledgements, links, archive, and deletion merge pure updates
against the same root ETag; a conflict recomputes from current state. Deleted
roots cannot be recreated by an update. Worker mutations still check their
execution ownership fence inside the transaction. Spawn admission and execution
acceptance retain storage ownership. Local coordinator state is derived bookkeeping.
Root and node HTTP input may carry a stable client message ID. A matching retry
joins the durable identity; different content or attribution fails closed.
Uncertain storage responses reconcile that exact identity and never replay an
unconfirmed mutation automatically. `deliver_session_message` returns the
committed `ChatMessage`; session inbox persistence projects it with `from_record`.
Already retained incoming records are observations, never another persistence.
SSE timeline notifications remain hints; cross-process timeline readers poll
durable cursors. Session execution separately loads complete history since its committed context checkpoint
plus every unconsumed input, then reads only inputs after its observed position.
A point-read inbox head avoids unchanged SQL reads. Sessions retain their selected
records and persisted outputs for arrival reconciliation; success-bound ACKs,
partial tool grouping, and the pure live/replay provider projection are unchanged.

Normal worker dispatch consumes compact Cosmos change-feed references rather than
repeated runnable-work SQL searches. The adapter establishes a server-issued feed
cursor before a startup snapshot of all retained pending/running references;
the overlap closes the snapshot/notification admission race without relying on
host clocks or an empty feed. The SDK's response hook retains the composite
continuation through empty/304 pages. Notifications do not hydrate payloads;
only the claimed attempt loads executable work. Process-local post-commit events
reduce wake latency, while independent processes consume the same durable feed.
Notifications may repeat. Workers retain references when capacity or ownership
is busy, and serialize each resource. Startup reconciliation
and a once-per-minute bounded pass recover missed work and expired attempts;
workflow dispatch repair uses the same minute cadence. No notification checkpoint
is required for correctness: restart snapshots recover retained work. The feed
pull uses short cursor polls, not catalog SQL polling or a new broker dependency.

Workers acquire a renewable resource claim with a token and increasing fence.
Every authoritative worker mutation checks that claim in the SAME Cosmos batch
as its data write. All runtime adapters share one catalog ContainerProxy and
workspace partition for execution documents and work. Immutable FCA submission
references live in their domain container and are retained before dispatch.
A stale owner cannot publish success, failure, tool results, or input ACKs.
External dispatch checks current authority; a call already in progress can still
finish remotely after lease loss, so retained job IDs remain essential.
Expired running attempts become interrupted and are never automatically replayed.
New operator input is independent work. Missing phase dispatch is repaired from
durable launches and accepted completion contracts, without rerunning completed
or interrupted phase work. The engine still never requests operator input.

`thesis_runtime/storage/ownership.py` and the WorkStore port contain execution authority;
Cosmos adapters implement transactions, not conversation policy. The serving
composition binds model, conversation, and workflow handlers. These boundaries
allow a future Rust core to own the inbox, claims, and phase contracts while
workspace and harness tools remain injected transports, including MCP hosts.

### Immutable submissions and tool contracts

Every serving harness mutation retains its allocated job_id and frozen inputs
before dispatch, independently of workflow ToolJob tracking. The Cosmos journal
is shared by API and worker replicas; local workspace composition uses SQLite.
Workspace submissions are inspectable at `/v1/workspaces/{workspace_id}/submissions`.
Unknown outcomes retain their original ID and are reconciled without replay.
HTTP backtest submission accepts inline `config`, returns 202, and never accepts
a mutable harness-host path. Screening submission returns 202; the caller waits
and explicitly materializes a completed retained result.

Session tools reject duplicate identities and malformed Draft 2020-12 schemas
at registration. Local references resolve without network retrieval; unsupported
remote references fail closed. Native harness operations which overlap workspace
program operations use stable `harness_` names. Workspace tools keep their domain
names. The optional path-only native `launch_trader` is withheld from session
catalogs until it supports immutable cross-host submissions.
Synchronous HTTP storage, authentication, and MCP work runs in FastAPI's thread
pool; streaming endpoints retain their asynchronous boundary.

Deployment cutover: workflow documents now live in the workspace catalog rather
than the old executionId partition. Drain active executions before deploying this
shape; no automatic replay or migration is introduced. Existing old-container
data is not deleted by runtime startup.


### Runtime latency verification boundaries

`tests/thesis/http/test_stateless_ownership.py` verifies atomic HTTP acceptance
without catalog queries or duplicate work creates, rollback without orphaned
messages/cursors/work, notification dispatch after API replacement and during
busy conversation ownership, complete scoped context and pending input, and unchanged-head reads without SQL. The external Cosmos fake models
transaction CAS/rollback, latest-version feed pages, and empty-page continuation.
`tests/thesis_runtime/test_arrival_reconciliation.py` verifies late steering,
explicit tool-result correlations, consumption ACKs, and exact live/replay
provider requests through both memory and Cosmos storage compositions, for root
and phase conversations. The required delivery architecture guard remains active.

### Harness completion transport configuration

`HARNESS_COMPLETION_URL` names Thesis's `/v1/internal/harness/completions` endpoint;
`HARNESS_COMPLETION_TOKEN` is its service bearer, retained only in configuration.
The local launcher wires the local endpoint and stable harness bearer. Azure
composition supplies the Thesis ingress URL and the existing harness bearer
secret to the API, finite execution workers and scheduled dispatcher. Thesis
accepts this service token separately from operator cookies/CSRF/workspace
membership. Unknown/non-conversation jobs are acknowledged without a model wake.
The endpoint accepts only compact terminal evidence, never model-chosen routing
or accepted source. Existing jobs without a conversation recipient are not
inferred or upgraded; newly retained agent submissions carry the current contract.
