# Thesis Workflow Development Instructions

Scope: any change under `src/thesis/workflows/**`, workflow tools, or phase
agents. Read the repository source directly:
`.github/architecture/thesis-agent-workflow-runtime.md` (REQ-018..REQ-040 and
"Option 1 Session Delivery Contract") and
`.github/instructions/agent-session-delivery.instructions.md`.

## Non-negotiables

1. **Agents own their turns.** The engine never calls a model for a phase,
   never drives a turn loop, never schedules a conversation. No turn drivers,
   no `advance_turn`, no engine-side prompts.
2. **Spawning is prompting.** Create and start an agent in one act:
   `Conversations.spawn(returns_to, spec, prompt, kind)` creates the durable
   record and delivers its first prompt at its own anchor. Never create an
   agent and separately schedule it.
3. **Transitions are requested.** A phase advances only when its executor
   requests it — an agent through `request_phase_completion`, deterministic
   code through its phase's returned contract. The engine validates
   (validator, required fields, target) and routes, rejecting back to the
   agent through the tool result for repair.
4. **Handoffs are explicit.** Cross-phase data travels only through the
   accepted completion contract, the declared pipes, and the start adapter. An
   agent NEVER reads another agent's transcript.
5. **Workflow-specific content stays in the workflow package**
   (`workflows/registry/<id>/`). `workflows/core/` stays generic; if your
   change adds workflow knowledge to `core/`, it is wrong.
6. **One writer, one path.** Every incoming producer, including dispatched tool
   results, MUST use `Conversation.deliver_session_message` and the singular
   neutral `SessionInbox`. Native provider roles MUST NOT be session message
   types. Live/replay MUST share the pure provider adapter. Intake APIs and
   visuals MAY differ; they MUST NOT introduce a second writer or inbox.
7. **Operator gates are prompted, not enforced.** Instruct the phase agent to
   ask via `request_workflow_input` and complete only on acceptance. Never add
   a deterministic gate to a workflow unless the operator specifically asked
   for that gate in the request that authorized the work: no engine-enforced
   confirmations and no validators whose job is to block an agent's requested
   transition until some conversational condition holds. Steering belongs in
   the phase prompt and the advertised tool schema, not the engine. Do not add
   engine policy, config fields, or gates whose only effect is prose. Trust
   the model; a model that ignores its phase prompt is unfit for this domain.
8. **Never invent an entity silently.** A new phase, agent, tool, or durable
   concept must be declared in the phase table, documented, and tested in the
   same change.
9. **Retired persisted values stay readable.** Durable records decode through
   closed tables; keep retired enum members and map them on read.
10. **Verify the owned scope.** Use the canonical `uv run --extra dev` commands
    in `.github/instructions/copilot-instructions.md` for focused Ruff lint,
    format checks, ty, and pytest. The integration owner runs repository-wide
    verification once after edits freeze and MUST include
    `tests/thesis/agent/runtime/test_delivery_architecture.py` once the delivery
    modules are final. Rewrite old-contract tests; do not keep two truths.

## Required reading before edits

- `.github/instructions/agent-session-delivery.instructions.md` (neutral inbox,
  canonical incoming writer, provider projection and enforcement).
- `.github/architecture/thesis-agent-workflow-runtime.md` (REQ-018..REQ-040,
  conversation abstraction, anchoring, and Option 1 delivery contract).
- `.github/architecture/repository.md` (package and dependency boundaries).
- `.github/architecture/thesis-ui-design.md` (shared timeline, scoped intake,
  and presentation versus execution authority).

## Current shape (do not regress)

The conversation service is implemented. Option 1's neutral message, inbox,
and provider-adapter modules are REQUIRED migration targets, not a claim of
completed implementation. Existing role-based session/projection code MUST NOT
be extended as an alternative design.

- One conversation abstraction: `thesis.agent.runtime.conversation` owns the
  `Conversation` handle and the `Conversations` service; HTTP, tools, and the
  engine see only the service and the handles. Nothing outside the service
  references the turn runner, coordinator, or spawner.
- A chat is a conversation at the root anchor; a sub-agent is a nested one.
  Phase specs are the sealed union `ConversationPhase` / `OneShotPhase` /
  `DeterministicPhase`; a deterministic phase builds no conversation at all.
- `ChildConversation` composes the return layer: `returns_to` anchor,
  complete/fail/result/await_result/children/cancel, and one turn-end policy
  (`OneShotPolicy` completes on the final answer; `ConversationalPolicy`
  stays open). Phase children never auto-complete: their completion is the
  engine-bound `request_phase_completion` tool.
- The service owns scheduling: every wake is a delivery routed through the
  service's routing rule (root to itself; a record anchor is durable-only;
  every other conversation runs on its one serialized worker).
- Phase table starts at `hypothesis_creation`; `requested` is retired.
- Phase agents are children of the durable workflow node; the node is a record
  and is never turned.
- Conversation agents accept input from birth; one-shot children complete on
  their final answer and deliver it to the return anchor; phase agents complete
  only through the completion tool.
- Child results deliver at the caller's anchor and wake it; `await_sub_agent`
  waits (bounded) or ends the turn.
- One turn worker per conversation; mid-turn wakes drain into the running
  session.
