# Durable agent runtime

This package owns conversation handles, durable turns, scoped tool dispatch,
and child return semantics. Workflow domain logic and workspace storage belong
to their respective packages, not to the conversation implementation.

- `__init__.py` exposes the runtime facade.
- `child.py` composes child return semantics over a conversation.
- `context.py` carries trusted tool-call attribution.
- `control.py` adapts control operations to the conversations service.
- `control_tools.py` binds control tool schemas and handlers.
- `conversation.py` owns conversation handles, scheduling, and routing.
- `datum.py` defines typed returned data.
- `delivery.py` persists and routes anchored deliveries.
- `json_recovery.py` repairs bounded model JSON decoding failures.
- `lifecycle.py` manages durable agent lifecycle records.
- `policy.py` defines spawn and operator-input authority.
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

Only serving composition injects strategy authoring into a verified design
executor. Inherited tool names alone give helpers no authoring authority.
