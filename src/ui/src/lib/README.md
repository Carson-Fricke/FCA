# Frontend data and controllers

This folder owns frontend transport, UI models, timeline projection, and chat
navigation. Backend admission rules, agent execution, and production capability
claims don't belong here.

| File | Purpose |
| --- | --- |
| `api.ts` | Endpoint facade, backend selection, product fixture mutations, and shared bounded versioned workflow-definition cache. |
| `transport.ts` | JSON HTTP transport and structured `ThesisApiError` with status, path, code, message, and details. |
| `wire.ts` | One cached decoder registry for HTTP, SSE, offline seeds, nested runtime metadata, and tool outcomes. |
| `generated.ts` | Generated backend wire types and schema-name-to-type registry. Don't edit manually. |
| `wire-schema.json` | Generated JSON Schema registry consumed by the production decoder. |
| `types.ts` | UI projections derived from generated chat, execution, timeline, tool, and node types; unrelated product mirrors remain scoped separately. |
| `chat-host.svelte.ts` | Shared conversation list, draft/create/post/delete, percolation, and timeline lifecycle controller used by both hosts. |
| `chat-navigation.ts` | Conversation URL adapter; clears scope and hash on conversation changes. |
| `timeline.svelte.ts` | One ordered timeline owner: history snapshot first, then cursor-only SSE, backward pagination, identity deduplication, and thread/execution indexes. |
| `fixture-timeline.ts` | In-memory offline timeline seeded from messages, events, and execution fixtures; mutations publish to subscribers. |
| `sse.ts` | Sole `TimelineStream` SSE client, with frame parsing, timeline URL construction, and reconnect cursors. |
| `workflow-live.ts` | Pure execution-indexed live workflow map; all lifecycle events replay against first-save snapshots, with explicit phase-executor projection, attention, and active/terminal selectors. |
| `workflow-scope.svelte.ts` | Execution-scoped phase navigation and event selection, node capability snapshots, history selection, and node submission. |
| `scope.svelte.ts` | Scope path serialization, storage, and navigation stack. |
| `subagent.ts` | Workflow and sub-agent event projections, including attempt freshness. |
| `transcript.ts` | UI transcript ordering, tool call/result matching, pending state, neutral wire-delivery presentation, and accepted research report decoding. |
| `workflow-graph.ts` | Workflow topology and display projection. |
| `workspace.svelte.ts` | Workspace-scoped view state and toasts. |
| `workspace-context.svelte.ts` | Workspace shell context: render-gate summary plus snapshot detail loaded after render. |
| `markdown.ts` | Sanitized Markdown rendering. |
| `format.ts` | Display formatting. |

`components/` contains presentation, `fixtures/` contains offline sample records,
and `data/` contains chart and graph demo data.

Full chat and copilot use one timeline stream per attached conversation. Live
cards select their own execution ID from the workflow map, not a singleton
`effectiveExecution`. Execution, chat-message, and sub-agent SSE clients/routes
are removed. REST execution and
sub-agent event replay remains available without creating another subscription.

Execution snapshots preserve first-save state and may arrive after lifecycle
events; their timeline sequence does not suppress earlier event replay. Phase
steps belong to declared workflow phases and explicit phase executors, not
helpers. Phase navigation uses execution ID plus canonical phase name.

Topology requests share a cache bounded to 64 entries, including tracked
in-flight reads, keyed by backend, API base URL, workspace, workflow ID, and
version. Matching explicit-version definitions expire after five minutes;
failures, mismatches, and unversioned latest reads are removed on completion.
The cache does not share mutable execution state. Presentation components pause
workflow-card canvas animation outside the viewport or when the tab is hidden.

## Generate wire artifacts from the backend export

Run `npm run generate:wire` from `src/ui` to consume the authoritative
`backend-wire.json` export. Explicit OpenAPI 3.1 `components.schemas` or JSON
Schema `$defs` paths can be supplied after `--`. The generator
at `../../generate-wire.mjs` writes `generated.ts` and `wire-schema.json` here.
Add `--check` to compare committed artifacts without writing them.

`npm run generate:wire -- --check` compares both generated artifacts byte for
byte. Synthetic schemas test generator mechanics; backend serializer captures
and the exported fixture API test production decoding. Backend export ownership
stays in `thesis.http.schema`; no frontend field inventory substitutes for it.

Thread lists/create records, full node-input receipts, steering, timeline pages/frames,
root messages, executions, tree wrappers/nodes, workflow definitions, and canonical
errors are decoded before UI adaptation. Nested tool
outcomes in messages and event extension payloads use the same registry. Offline
sample records are aligned with the complete wire shape and validated, not
silently accepted through a looser fixture contract.

Neutral delivered datums have no model tool-call identity. The presentation
adapter retains them with null call/tool fields without modifying canonical
wire records. Nullable message metadata remains nullable in the backend schema
and the generated UI types; optional canonical fields are checked on object
metadata only.

Tree records retain kind, completion mode, phase executor, return anchor, tools,
budget, contracts, results, attempt ID, spec version, and timestamps. Context
isn't stored: only explicit event context can populate the UI projection, and a
durable node starts with `context: null`.

Thread records preserve the required `input_consumed_positions` map and nested
`links`; thread kind and flattened link lists are UI projections. Node receipts
validate the entire `NodeInputReceiptWire`, including `OperatorPolicyDecisionWire`,
before separating receipt-only attribution. Steering normalization maps generated
`SteeringPercolationWire` targets without a parallel handwritten validator.

Offline content hashes are sample identifiers, not claims of backend-issued
provenance. Serializer capture tests use untouched Python-produced hashes and
timestamps. The UI doesn't apply backend business validation or re-hash real
responses.
