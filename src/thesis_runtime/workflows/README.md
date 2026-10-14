# thesis_runtime/workflows

Generic topology, lifecycle, accepted history, registration and the versioned JSON extension boundary. Domain requests, reducers, activities and reports belong to extensions.

- `__init__.py`: The general Thesis workflow implementation.
- `builder.py`: Assemble and validate a workflow specification and its general object.
- `delivery.py`: Node lifecycle.
- `events.py`: Generic frontier, waiting-join and lifecycle event projections.
- `execution_store.py`: The execution store: one durable document per workflow execution (REQ-B5).
- `extensions.py`: Defines detached versioned JSON requests, trusted execution binding and structural state decoding for workflow extensions.
- `graph/`: Pure fixed split/join topology validation and accepted-history control projection, shared by serial execution and recovery.
- `phase.py`: Executable phase vocabulary and fixed structural split specifications.
- `registry.py`: Resolve each workflow ID to its single current registration and reject outdated versions.
- `spec.py`: Workflow specification and execution-context contracts.
- `state.py`: Common workflow state shapes and replay constants.
- `workflow.py`: The single general workflow object.
