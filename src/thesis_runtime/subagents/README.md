# thesis_runtime/subagents

Generic delegation, lifecycle, policy and operator/completion controls for child and phase agents. Domain phase policy is supplied by extensions.

- `__init__.py`: subagents capability package.
- `child.py`: A conversation plus child semantics, by composition.
- `control.py`: Thesis control tools.
- `control_tools.py`: Thesis-owned chat control tools: spawn, await, and operator input.
- `lifecycle.py`: Durable sub-agent lifecycle: spawn, input, complete, fail, and cancel.
- `policy.py`: Spawn authorization and per-role tool policy.
- `spawn.py`: Spawn an agent and give it its first prompt in one durable operation, retaining instruction text once with compact delivery metadata.
- `specs.py`: Declarative agent specifications and budgets for durable sub-agents.
- `store.py`: Durable stores for sub-agent records and first-class invocations.
- `tree.py`: Chat-scoped projection of the durable sub-agent hierarchy.
