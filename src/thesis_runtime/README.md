# thesis_runtime

Generic host for durable conversations, tools, workflows and subagents. It must not import FCA domain packages. Composition supplies providers, extensions and persistence.

Subdirectories: `adapters/`, `contracts/`, `conversations/`, `execution/`, `hosting/`, `sessions/`, `storage/`, `subagents/`, `tools/`, `workflows/`. Each documents its local boundary.

- `__init__.py`: Public generic runtime exports for tools, workflows, conversations and subagents.
