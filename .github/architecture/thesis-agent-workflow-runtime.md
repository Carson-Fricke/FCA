# Thesis Durable Agent and Workflow Runtime v1.0.0

The key words "MUST", "MUST NOT", "REQUIRED", "SHALL", "SHOULD",
"SHOULD NOT", "RECOMMENDED", "MAY", and "OPTIONAL" in this document are to
be interpreted as described in RFC 2119.

## Status

This document is the accepted design baseline for the next Thesis agent and
workflow runtime. Where the current implementation differs, this document
defines the target architecture.

### Implemented so far

Wave 1 (core contracts) and the durable sub-agent lifecycle are implemented in
`thesis.agent.runtime`:

- `SubAgentRecord` and `AgentInvocation` are durable `WorkspaceRecord` types,
  persisted through the shared chat persistence port, so the in-memory,
  filesystem (local), Cosmos, and Azure blob backends all carry them;
- `SubAgentSpec`/`SubAgentBudget` fix the phase kind, default-deny tool surface,
  completion contract, and delegated budget;
- `SubAgentLifecycle` enforces chat/workspace scope, maximum depth, maximum
  active children, delegated-budget inheritance, and the one-shot input rule;
- `PhaseCompletionProtocol` implements the nested budgets exactly: three
  construction attempts per agent turn, two repair turns, then operator
  escalation, with durable repair epochs;
- `ControlToolService` exposes the Thesis control tools, including
  `spawn_sub_agent` recorded as a first-class tool invocation;
- `SubAgentNotifier` delivers a child's result to its parent asynchronously by
  appending a durable `tool` message and scheduling a new parent turn.

Still to implement: the unified `AgentSession` turn loop, the harness
`run_study` MCP tool, phase-completion wiring into the workflow gates,
the nested `hypothesis_development` package, and removal of the sibling
workflows.

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
- Preserve the current hypothesis-creation and verdict user experience.
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
| Repair turn | A durable system turn that explains why no completion request was accepted and asks the same sub-agent to try again. |
| Study | A durable replay execution with no order execution, used to run indicators and research models. A study may fit and observe one research model, or run indicators alone. It is never represented as a backtest or a strategy. |
| Tool context | Trusted attribution attached by the runtime to a tool invocation. Models cannot provide or override it. |

## Requirements

**REQ-001: Deterministic phase machine.** A workflow MUST define an explicit,
versioned set of phases and legal transitions. Only deterministic workflow code
MAY persist a `PhaseTransition`.

- Rationale: Model output can propose an outcome but cannot be the authority
  for workflow legality.
- Verification: Attempting an undeclared transition fails without changing the
  execution phase.

**REQ-002: Three phase kinds.** Every phase MUST be declared as exactly one of
`conversation`, `one_shot`, or `deterministic`.

- Verification: Workflow registration rejects a missing or unsupported phase
  kind.

**REQ-003: Sub-agent foundation.** Every conversation or one-shot phase MUST
run through a durable sub-agent attached directly to the execution's chat. The
phase attempt MUST reference the sub-agent id. A deterministic phase MUST NOT
create a sub-agent solely to represent its execution.

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

**REQ-007: No model-selected target.** `request_phase_completion` MUST NOT
accept a target phase. A completion contract MAY contain domain intent such as
`continue` or `loop_back`; deterministic gate code maps that intent to a legal
transition.

**REQ-008: Complete durability.** All sub-agent state, conversation turns,
model invocations, tool invocations, results, completion requests, validation
results, and workflow transitions MUST be persisted before their effects are
treated as visible or complete.

**REQ-009: Caller receives tool result.** Every tool invocation MUST append a
durable `tool` turn to the conversation of the agent that invoked it. Tool
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
  kind                         # conversation | one_shot
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

Each sub-agent owns a durable ordered conversation. Supported turn roles are
`system`, `user`, `assistant`, and `tool`. Assistant tool-call requests and
their tool results MUST appear in the order seen by the model. A turn MAY link
to one or more first-class invocation records.

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
6. Persist each tool invocation and result before returning the result to the
   model as a `tool` turn.
7. Repeat until the model yields, a control tool is accepted, a process fault
   occurs, or a safety budget stops the turn.
8. Save a checkpoint after each durable boundary so recovery never repeats a
   completed side effect.

The current separate single-shot workflow model path and chat-only tool loop
MUST converge on this abstraction.

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
7. Return the gate result to the calling agent as a durable tool turn.
8. If the result is `advance`, `loopback`, or `fail`, apply the corresponding
   deterministic state-machine action after the tool result is durable.

An `advance`, `loopback`, or `fail` result MUST end the current phase-agent
turn. A `remain` result keeps the turn active and MAY be followed by evidence
gathering, another completion request, or an operator-input request.

An acceptance failure caused by missing or contradictory evidence is not a
schema construction failure. It MUST return `remain` with violations and leave
the phase active so the agent can call more tools, revise its evidence, request
operator input, or submit again.

### Nested Repair Budgets

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

## Harness Study Tool

Quant-harness MUST publish `run_study` as a real MCP tool. A **study** is a
durable replay execution with no order execution, used to run indicators and
research models: it may fit and observe one research model, or run indicators
alone. The tool replaces the synthetic `harness.research_model` label and
Thesis-side direct invocation of `ResearchModelRunner` from workflow code.

### A Study Is Not A Backtest

A study is a statistical replay, not a trading strategy. When it runs a research
model, that model's contract is `fit(context)` plus `observe(fit, context)`
returning finite named statistics, and its context MUST expose no order,
position, sizing, portfolio, or venue capability. A backtest's contract is entry
and exit logic producing fills with portfolio accounting, costs, and risk.

Running a study through the backtest boundary is prohibited because it conflates
two different contracts and produces a "backtest run" record, manifest, artifact
kind, and tool name for what is a statistical replay. That corrupts the
experiment ledger, the evidence-grade taxonomy, and the operator-visible report.

The deterministic point-in-time replay substrate (data ensure, point-in-time
resolution, session calendars, corporate actions, artifact publication, and run
persistence) MAY be reused. Reuse MUST be through a distinct study execution
mode that binds a no-order context and a distinct study run kind. A research
model MUST NOT be represented as a `Strategy`, and
`ResearchModelAdapter(Strategy)` is debt that MUST be removed. Studies MUST have
their own study record, manifest, and artifact kind so the ledger, evidence
grade, and UI label them as studies.

The phase acceptance gate MUST bind falsifiers to the study's recorded metric
references, never to a backtest run.

### Input

```json
{
  "model_ref": "models/<model-id>/<content-sha>",
  "experiment_contract_ref": "experiments/<contract-id>/<sha>",
  "data": {
    "symbols": ["SPY"],
    "start": "2020-01-01",
    "end": "2025-12-31",
    "frequency": "1d"
  },
  "requested_observables": ["sample_valid", "trend_strength"],
  "configuration": {}
}
```

The trusted tool context supplies attribution and idempotency. Those fields
MUST NOT appear in model-controlled arguments.

### Execution

The tool MUST:

1. Resolve and verify the content-addressed research model and indicator refs.
2. Resolve point-in-time data under the experiment contract.
3. Run `fit(context)` exactly once.
4. Publish immutable fitted parameters and calibration bounds.
5. Run `observe(fit, context)` against the same resolved historical context.
6. Validate that requested outputs are finite named numeric values.
7. Publish run, data-manifest, fit, metric, sample, and diagnostic artifacts.
8. Record trusted chat, conversation, sub-agent, workflow, phase, and
   invocation attribution.

An inadequate sample or unidentifiable fit SHOULD return explicit model-defined
numeric failure observables such as `sample_valid=0.0`. Infrastructure, policy,
or model-contract failures MUST return a typed failure and MUST NOT fabricate
metrics.

### Result

```json
{
  "study_id": "study-...",
  "state": "succeeded",
  "study_digest": "<sha256>",
  "model_ref": "models/<model-id>/<content-sha>",
  "model_artifact_ref": "artifacts/...",
  "indicator_refs": [],
  "fitted_model_ref": "models/fits/<fit-sha>.json",
  "fitted_parameters_ref": "artifacts/...",
  "data_manifest_ref": "artifacts/...",
  "runtime_configuration": {},
  "observations": {
    "sample_valid": 1.0,
    "trend_strength": 0.18
  },
  "observation_artifacts": [],
  "failure": null,
  "attribution": {
    "chat_id": "...",
    "conversation_id": "...",
    "sub_agent_id": "...",
    "workflow_execution_id": "...",
    "phase_attempt_id": "...",
    "tool_invocation_id": "..."
  }
}
```

The result MAY omit large parameter and sample values in favor of immutable
references. The completion gate MUST resolve refs and metrics from the durable
run store; it MUST NOT trust copied values in model prose or completion JSON.

## Hypothesis Development Workflow

The current operator experience remains unchanged, but phase wiring changes as
follows.

| Phase | Kind | Agent behavior | Completion contract and deterministic action |
| --- | --- | --- | --- |
| `requested` | deterministic | None | Validate intake; advance to creation or fail. |
| `hypothesis_creation` | conversation | Propose the statistical model, falsifiers, controls, and Layer B monitors. Yield so the operator can confirm or revise. After clear confirmation, call `request_phase_completion`. | Construct `HypothesisCreationCompletion`; apply falsifiability, model-output, and plan gates; advance to trials or remain. |
| `falsification_trials` | one-shot | Call `run_study` and artifact tools directly, inspect results, author the interpretation, then request completion. | Bind every falsifier/control to a recorded run metric and artifact; advance to verdict or remain. |
| `verdict` | conversation | Present the recorded deterministic verdict and ask whether to continue or loop back. After the operator replies, submit interpreted intent. | Construct `VerdictCompletion`; verify it refers to the latest operator message and verdict; map `continue` or `loop_back` through the transition table. |
| `strategy_design` | one-shot | Read the surviving model and fit, write the strategy, call validation tools, then request completion. | Validate bundle refs, sandbox result, and falsifier-bound Layer B exits; advance or remain. |
| `strategy_backtesting` | one-shot | Call harness backtest and artifact tools directly, interpret the result, then request completion. | Resolve the recorded run; advance to optimization on success or loop to strategy RCA on failure. |
| `strategy_rca` | one-shot | Inspect the failed run, spawn critics if useful, author a diagnosis and revision guidance, then request completion. | Validate diagnosis and revision refs; loop to strategy design or remain. |
| `optimization` | one-shot | Call sweep/backtest tools directly within declared bounds, inspect results, and request completion. | Resolve the recorded optimization run; complete on success or loop to strategy RCA on failure. |
| `completed` | deterministic | None | Persist terminal artifacts and completion state. |

### Hypothesis Creation Completion

The contract MUST preserve the current structured hypothesis, research model,
and falsification-plan fields. It MUST include:

- claim, disagreement, risk factor, horizon, and universe;
- falsifiers with id, observable, condition, threshold, and exact test;
- the content-addressable research model source, outputs, tests, indicators,
  and harness configuration;
- negative or placebo controls;
- Layer B monitor mappings; and
- a concise operator-confirmation reference to the latest confirming message.

The phase agent asks whether the proposal matches the operator's intent by
ordinary prose and yields. Feedback causes another proposal turn. Confirmation
causes the agent, not keyword-matching workflow code, to submit the completion
contract.

### Falsification Trials Completion

The contract MUST include:

- hypothesis and research-model refs;
- `run_study` study id and digest;
- fitted-model ref;
- one evidence binding per falsifier;
- one binding per required control;
- observed metric refs, not copied unsupported values;
- interpretation artifact ref; and
- limitations or typed failures.

The gate MUST verify every binding against a successful or explicitly failed
tool result in the same phase attempt. A failed harness call is an execution
problem, not automatic hypothesis refutation.

### Verdict Completion

The verdict remains derived deterministically from recorded falsifier evidence.
The model submits only the operator's intent:

```json
{
  "verdict_ref": "verdict-...",
  "operator_message_id": "message-...",
  "operator_intent": "continue",
  "summary": "The operator accepted the recorded verdict."
}
```

`operator_intent` is `continue` or `loop_back`. The gate verifies that the
message is the latest applicable operator turn and that the interpretation is
consistent with it. Ambiguity returns `remain` or causes the model to call
`request_operator_input`; no substring marker classifier is used.

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

## Target Package Layout

The implementation MUST replace monolithic workflow modules with nested
packages. Public compatibility facades MAY temporarily re-export existing
names during migration.

```text
src/thesis/agent/
  session/
    __init__.py
    session.py          # durable model/tool turn loop
    turn.py             # durable turn contracts
    history.py          # replay and resume assembly
    budgets.py          # construction, repair, tool, token, and time budgets
    errors.py
  tools/
    __init__.py
    protocol.py         # common tool spec/result contracts
    registry.py         # merged catalog
    control.py          # phase/sub-agent completion and operator input
    spawn.py            # spawn_sub_agent, await_sub_agent
    harness.py          # harness MCP adapter
    dispatch.py         # policy-scoped dispatch
  invocation/
    __init__.py
    provider.py         # tool-capable provider boundary
    completion.py       # model completion and tool calls
    provenance.py       # first-class invocation records
  subagents/
    __init__.py
    records.py          # durable sub-agent contracts/store protocol
    lifecycle.py        # spawn, input, complete, fail, cancel
    tree.py             # chat/sub-agent hierarchy projection
  router.py
  skills.py
  context.py

src/thesis/workflows/
  core/
    __init__.py
    phase.py            # AgentSpec, PhaseKind, PhaseSpec, PhaseRegistry
    machine.py          # transitions and PhaseTransition
    state.py            # common workflow state/replay
    gate.py             # deterministic acceptance protocol/outcomes
    checkpoint.py       # pause/resume integration
    runner.py           # generic state-machine runner
    events.py           # workflow event projections
  hypothesis_development/
    __init__.py         # public facade
    workflow.py         # thin phase registry and state-machine wiring
    request.py
    state.py
    artifacts.py
    ledger.py
    phases/
      creation.py
      trials.py
      verdict.py
      design.py
      backtesting.py
      strategy_rca.py
      optimization.py
    contracts/
      creation.py
      hypothesis.py
      plan.py
      trials.py
      verdict.py
      strategy.py
      rca.py
    gates/
      falsifiability.py
      research_model.py
      plan.py
      evidence.py
      strategy.py
    prompts/
      creation.py
      falsification.py
      strategy.py
      rca.py
    loops/
      premise.py
      strategy.py
  strategy_development/
    phases/
    contracts/
    gates/
    prompts/
  discovery/
    phases/
    contracts/
    gates/
    prompts/
  rca/
    phases/
    contracts/
    gates/
    prompts/
  runtime.py            # slim composition root
```

`src/thesis/execution/sub_agents.py` SHOULD become a temporary compatibility
facade over `thesis.agent.subagents`. `src/thesis/execution/checkpoints.py`
remains the snapshot substrate. The execution event log remains the streaming
and projection mechanism, while first-class invocation and sub-agent stores are
their own durable sources of truth.

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
- Add depth, child-count, budget-inheritance, authorization, and resume tests.

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
