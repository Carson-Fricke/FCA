# Thesis provider boundary

This package owns backend-only model profiles, credentials, native request
contracts, capability checks, and transport. It serves the neutral agent runtime;
it does not own conversation intake, persistence, scheduling, consumption ACKs,
tool execution, or workflow policy.

- `__init__.py` exposes provider profiles, capability checks, and invocation ports.
- `foundry.py` implements credential-injected Azure AI Foundry profiles,
  capability preflight, and completion boundaries.
- `invocations.py` defines immutable native invocation/message/call contracts,
  model-profile role bindings, completion parsing, and typed provider errors.
- `openrouter.py` implements the fail-closed OpenRouter completion adapter.
- `session_adapter.py` defines `NativeSessionAdapter` and pure
  `project_native_messages` for neutral-to-native projection shared by live/replay.
- `transports.py` implements concrete local network transports.

`NativeSessionAdapter.request_snapshot` purely prepares detached actual native
messages, advertised tools, options, and projection version (`native-session-v1`).
The completion wrapper uses the same preparation, calls injected
`provider.complete`, and returns a neutral outgoing `SessionMessage`. Projection
has no effects, runtime callbacks, or hidden service introspection.

Tool results join only on explicit request message ID, request ID, and tool name
within the supplied conversation. Partial/ambiguous groups stay evidence without
fabricated calls/results; grouping never changes durable arrival order. Native
roles stay at this boundary; source labels do not grant instruction authority.

The session persists `provider_request` as opaque audit data alongside neutral
`request_messages` and hashes in a `STARTED` invocation before provider execution,
preserving it on terminal updates. Native audit fields allow storage/inspection,
not runtime role mapping, new session types, or independent delivery. Required
verification is in `.github/architecture/thesis-agent-workflow-runtime.md`
(`REQ-DEL`), including `tests/thesis/providers/test_session_adapter.py` and the
mandatory coordinator-owned runtime source guard.
