# thesis_runtime/tools

Provider-neutral tool catalog, dispatch, argument validation, generic workflow launch and hosted-tool transport. Concrete FCA tools belong in thesis/extensions/tools.

- `__init__.py`: tools capability package.
- `json_recovery.py`: Tolerant JSON extraction from model output.
- `registry.py`: Provider-neutral tool descriptors, explicit trusted invocation, and dispatch policy.
- `remote.py`: Adapts a hosted-tool JSON transport into normal dispatch with trusted context, exact acknowledgement and unknown-outcome handling.
- `schema.py`: Pure Draft 2020-12 validation shared by tool dispatch and workflows, with exact nested paths and concise constraint/expected/received feedback, including property names, union failures and length limits without large offending-value echoes.
- `workflows.py`: Launch any registered workflow using its own validated input schema.
