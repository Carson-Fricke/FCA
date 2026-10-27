# thesis_runtime/workflows

The generic graph engine is isolated in `graph/`. This package also contains its conversation-facing host, durable history, registration and JSON extension adapters. Domain requests, reducers, activities and reports belong to extensions.

- `__init__.py`: The general Thesis workflow implementation.
- `builder.py`: Assemble and validate a workflow specification and its general object.
- `delivery.py`: Node lifecycle.
- `events.py`: Host frontier, pending-input and lifecycle event projections.
- `execution_store.py`: The execution store: one durable document per workflow execution (REQ-B5).
- `extensions.py`: Defines detached versioned JSON requests, trusted execution binding and structural state decoding for workflow extensions.
- `graph/`: Agent-independent named-port topology and activation replay, shared by completion and recovery.
- `phase.py`: Host activity bindings (conversation, one-shot and deterministic), named input metadata and terminal outcomes.
- `registry.py`: Resolve each workflow ID to its single current registration and reject outdated versions.
- `spec.py`: Workflow specification and execution-context contracts.
- `state.py`: Common workflow state shapes and replay constants.
- `workflow.py`: Conversation-facing workflow host: activity dispatch, trusted lifecycle acceptance and projections over the generic graph.
