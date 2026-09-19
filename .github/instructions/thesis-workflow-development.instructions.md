# Thesis Workflow Development Instructions

Scope: any change under `src/thesis/workflows/**`, workflow tools, or phase
agents. Read the authoring guide first:
`cleo docs fetch thesis-workflow-authoring-guide`. Deep contracts:
`.github/architecture/thesis-agent-workflow-runtime.md` (REQ-018..REQ-033).

## Non-negotiables

1. **Agents own their turns.** The engine never calls a model for a phase,
   never drives a turn loop, never schedules a conversation. No turn drivers,
   no `advance_turn`, no engine-side prompts.
2. **Spawning is prompting.** Create and start an agent in one act:
   `SubAgentSpawner.spawn(...)` creates the durable record and delivers its
   first prompt at its own anchor. Never create an agent and separately
   schedule it.
3. **Transitions are requested.** A phase advances only when its executor
   requests it — an agent through `request_phase_completion`, deterministic
   code through its executor's returned contract. The engine validates
   (validator, required fields, target) and routes, rejecting back to the
   agent through the tool result for repair.
4. **Handoffs are explicit.** Cross-phase data travels only through the
   accepted completion contract, the declared pipes, and the start adapter. An
   agent NEVER reads another agent's transcript.
5. **Workflow-specific content stays in the workflow package**
   (`workflows/registry/<id>/`). `workflows/core/` stays generic; if your
   change adds workflow knowledge to `core/`, it is wrong.
6. **One writer, one path.** One tool-result payload shape, one delivery path,
   one spawn path. Collapse duplicates instead of syncing them.
7. **Operator gates are prompted, not enforced.** Instruct the phase agent to
   ask via `request_workflow_input` and complete only on acceptance. Do not add
   engine policy, config fields, or gates whose only effect is prose. Trust the
   model; a model that ignores its phase prompt is unfit for this domain.
8. **Never invent an entity silently.** A new phase, agent, tool, or durable
   concept must be declared in the phase table, documented, and tested in the
   same change.
9. **Retired persisted values stay readable.** Durable records decode through
   closed tables; keep retired enum members and map them on read.
10. **Verify after every step:** `uv run --no-sync --extra dev ruff check
    src tests scripts`, `ruff format`, `ty check src/thesis`,
    `pytest -p no:cacheprovider tests/thesis tests/harness -q`. Never leave a
    red tree. Rewrite old-contract tests; do not keep two truths.

## Required reading before edits

- `cleo docs fetch thesis-workflow-authoring-guide` (working rules, phase
  contract, testing requirements, anti-patterns).
- `cleo docs fetch thesis-chat-surface-and-subagent-anchoring-v1` (the
  inversion design record).
- `.github/architecture/thesis-agent-workflow-runtime.md` REQ-018..REQ-033.
- `cleo memory find "workflow"` for recorded pitfalls.

## Current shape (do not regress)

- Phase table starts at `hypothesis_creation`; `requested` is retired.
- Phase agents are children of the durable workflow node; the node is a record
  and is never turned.
- Conversation agents accept input from birth; one-shot children complete on
  their final answer and deliver it to the caller; phase agents complete only
  through the completion tool.
- Child results deliver at the caller's anchor and wake it; `await_sub_agent`
  waits (bounded) or ends the turn.
- One turn worker per node; mid-turn wakes drain into the running session.
