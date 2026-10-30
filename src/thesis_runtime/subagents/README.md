# thesis_runtime/subagents

Generic delegation, lifecycle, policy and operator/completion controls for child and phase agents. Domain phase policy is supplied by extensions.

- `__init__.py`: subagents capability package.
- `child.py`: A conversation plus child semantics, turn-end completion policy, transcript joins for pending child returns, and canonical cancellation-result delivery.
- `control.py`: Thesis control tools.
- `control_tools.py`: Thesis-owned chat controls for spawning, completion and operator input. Child results arrive through canonical delivery without wait tools or blocking SDK helpers.
- `lifecycle.py`: Durable sub-agent lifecycle: spawn, input, complete, fail, and cancel.
- `policy.py`: Spawn authorization and per-role tool policy.
- `spawn.py`: Persist explicit initial context before the runnable assignment through canonical delivery. Source-keyed context is copied once per child; retried/revisited assignments remain independently idempotent.
- `specs.py`: Declarative agent specifications, budgets and immutable initial-context values with explicit source-message/conversation provenance.
- `store.py`: Durable stores for sub-agent records and first-class invocations.
- `tree.py`: Chat-scoped projection of the durable sub-agent hierarchy.
