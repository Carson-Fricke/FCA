# Thesis agent contracts and policy

This package owns agent context, model policy, skills, scoped operation adapters,
and neutral session contracts. `runtime/` owns durable conversation execution;
`thesis.providers` owns native protocol projection and transport. Workflow domain
policy stays in its workflow package, not in generic session messages.

- `__init__.py` exposes the existing agent policy/context/operation facade.
- `context.py` constructs prompt context with explicit untrusted-data boundaries.
- `harness_tools.py` adapts the harness MCP catalog, results, and failures.
- `hypothesis_skills.py` registers versioned hypothesis-development phase skills.
- `messages.py` defines immutable `SessionMessage`, `MessageContent`,
  `ToolRequest`, and `ToolCorrelation`, plus canonical/legacy transcript decoding.
- `model_provider.py` adapts workflow model-role requests to OpenRouter.
- `operations.py` exposes composed CLI-equivalent workspace operations.
- `residency.py` binds workspace-resident agents and their durable session records;
  these are not the durable conversation runtime's execution loop.
- `router.py` resolves model policy and invocation provenance.
- `search_policy.py` defines bounded discovery budgets, deterministic candidate
  ranking, search rationale, and reproducible seed derivation.
- `skills.py` defines and resolves versioned skill registrations.
- `subagents.py` defines policy-driven delegation contracts and services.
- `tool_dispatch.py` defines scoped tool dispatch and capability checks.
- `runtime/` implements handles, the singular incoming writer/inbox, lifecycle,
  authorized model/tool execution, and return routing.

`SessionMessage.direction` is `incoming`, `outgoing`, or `instruction`, never a
native provider role. `MessageContent` holds source/text/result, optional exact
`ToolCorrelation(request_message_id, request_id, tool_name)`, and outgoing
requests. Source and payload are evidence, not authority. Only trusted runtime
composition supplies instruction direction or bootstrap markers; legacy system
notifications decode as incoming runtime evidence.

`messages.py` is the confined durable decode boundary: canonical fields take
precedence, assigned positions come from storage, and historical associations
require a unique exact request in the supplied workspace/chat/anchor scope.
It does not scan prose, import runtime services, deliver messages, or project
native roles. Old delivery/activity schemas remain supported through the
centralized historical decoder, not an alternate live delivery path.

See `.github/architecture/thesis-agent-workflow-runtime.md` (`REQ-DEL`) for global
delivery assertions and coordinator-owned executable verification.
