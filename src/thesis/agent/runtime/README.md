# Durable agent runtime

This package owns conversation handles, durable turns, scoped tool dispatch,
and child return semantics. Workflow domain logic and workspace storage belong
to their respective packages, not to the conversation implementation.

- `__init__.py` exposes the runtime facade.
- `activity.py` defines immutable runtime activity/schema contracts and the
  centralized historical metadata decoder.
- `child.py` composes child return semantics over a conversation.
- `context.py` carries trusted tool-call attribution.
- `control.py` adapts control operations to the conversations service.
- `control_tools.py` binds control tool schemas and handlers.
- `conversation.py` owns conversation handles, scheduling, and routing.
- `datum.py` defines typed returned data.
- `delivery.py` adapts anchored datum delivery to the canonical incoming writer
  and routes wakes; it does not own another storage path.
- `inbox.py` owns `SessionInbox`, injected durable incoming delivery, ordered
  pending snapshots, output bookkeeping, and success-bound consumption.
- `incoming.py` owns `write_incoming`, the singular incoming storage boundary.
- `json_recovery.py` repairs bounded model JSON decoding failures.
- `lifecycle.py` manages durable agent lifecycle records.
- `knowledge_tools.py` binds context-scoped retrieval tools to knowledge ports.
- `policy.py` defines spawn and operator-input authority.
- `program_tools.py` binds committed study, strategy, and indicator operations
  and bounded SDK-reference reads to trusted workspace context. Validation/run
  accept IDs, not inline source or paths; the existing dispatcher delivers results.
- `registry.py` composes injected tools under the effective session policy.
- `schema.py` validates advertised tool arguments.
- `session.py` owns the durable model/tool loop and tool-result envelope.
- `spawn.py` implements child creation collaborators.
- `specs.py` defines immutable child specifications and budgets.
- `store.py` adapts agent and invocation records to chat persistence.
- `strategy_tools.py` adapts the strategy domain service to a context-bound
  tool. It never selects paths, trusts model-supplied scope, or owns artifacts.
- `tree.py` projects durable agent trees.
- `turns.py` serializes and coalesces conversation turns.
- `workflow_tools.py` adapts workflow launch tools.

Serving composes workspace program tools for non-workflow conversations.
Workflow bindings grant program access only to selected owning phase executors;
inherited tool names alone give workflow helpers no program authority. Program
binding replaces conflicting raw harness and legacy strategy-authoring tools.

Strategy backtesting remains a one-shot phase agent: it reads the handed-off
program ID, runs latest committed content with current accepted validation, and
requests completion itself. SDK-reference reads use packaged canonical Cargo/Rust
assets; shared minimal templates belong to the workspace program service, not
the conversation runtime.

`Conversations.deliver_session_message` and its handle delegate reach
`incoming.write_incoming`; live tool results reach it through the injected
`SessionInbox.deliver` persistence port. `_persist_turn` writes outgoing envelopes
only, never another copy of observed incoming results. A multi-datum outcome is
one incoming receipt with an ordered array, expanded purely by the UI.

Sessions use `thesis.agent.messages` contracts, not native provider histories.
Trusted runtime composition supplies instructions; legacy system notifications
remain incoming evidence. The provider adapter purely prepares the versioned
native request. The session persists its opaque `provider_request`, reference-only
`request_messages` (`message_id`, `direction`, `position`), and hash in a `STARTED`
invocation before execution and preserves them on terminal updates, without
interpreting native audit roles.
Canonical bodies stay in the neutral transcript, not a full duplicate audit.
Cosmos persists the actual request as immutable chunks before its invocation
manifest and hydrates it without changing the runtime contract.
ACK follows provider success and outgoing persistence and covers only the pending
request snapshot, not late mid-model input or results after terminal requests.

Completion checks verify committed artifact references and stored validation;
they do not compile or repair source. The three-compiler-repair escalation is
workflow prompt guidance, not a runtime cap. The session iteration ceiling and
cancellation are independent safety limits.

Root/model-only execution and single-process scheduling remain shared. Steering
uses the canonical factory with `schedule=False`, then the ready callback after
epoch effects. Completion guards, store CAS, and deterministic-failure policy
remain unchanged. Global boundaries and required coordinator-owned verification
are in `.github/architecture/thesis-agent-workflow-runtime.md` (`REQ-DEL`) and
`.github/instructions/agent-session-delivery.instructions.md`; module presence or
instructions alone do not prove compliance.
