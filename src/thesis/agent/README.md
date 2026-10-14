# thesis/agent

FCA model routing, skills and harness/domain collaborators. Generic sessions, conversations, tool dispatch and subagent lifecycle live in thesis_runtime.

- `__init__.py`: Policy-driven Thesis agent routing, context, skills, and subagents.
- `context.py`: Precedence-aware prompt construction with explicit data boundaries.
- `harness_tools.py`: Harness MCP tool provider for the Thesis agent.
- `hypothesis_skills.py`: Versioned per-phase skills, including discovery's constructive research loop and conversational steering; formal model creation is explicitly phase-scoped.
- `operations.py`: CLI-equivalent workspace operations facade over composed services.
- `residency.py`: Workspace-resident agent binding with durable session records.
- `router.py`: Policy-driven model-role routing and invocation provenance.
- `search_policy.py`: Deterministic bounded search policy for autonomous strategy discovery.
- `skills.py`: Versioned, content-addressed Thesis skills.
- `subagents.py`: Fresh, workspace-scoped subagent context creation.
- `tool_dispatch.py`: Role-policy helpers for the quant-harness MCP tool surface.
