# Thesis HTTP Adapters

This package composes the Thesis HTTP application and adapts authenticated
requests to chat, conversation, workflow, workspace, and harness services.
It owns HTTP transport and wire contracts, not agent turn loops, provider
projection, workflow phase policy, or a second incoming-message writer.

## Files

| File | Purpose |
| --- | --- |
| `__init__.py` | Public application factory, authenticators, chat router, and chat request-model facade. |
| `application.py` | FastAPI factory, shared `/v1` and `/api/v1` registration, authentication, service composition, and single-worker serving enforcement. |
| `auth.py` | Entra OIDC login, server-side session cookies, session management, and CSRF protection. |
| `invites.py` | Invite lifecycle endpoints. |
| `errors.py` | Shared typed HTTP error translation and fail-closed service resolution. |
| `chat.py` | Workspace chats, messages, attachments, investigations, execution controls, tool jobs, and REST execution-event replay. |
| `chat_sub_agents.py` | Chat-scoped durable nodes, hierarchy, invocation/message/event reads, and anchored input admission. |
| `timeline.py` | Unified chat timeline REST history and SSE tailing of messages, events, and execution snapshots. |
| `streaming.py` | Timeline SSE media type, sleeper port, and reconnect cursor validation. |
| `serving.py` | Composition of chat, conversation, and workflow session services for local and deployed HTTP. |
| `schema.py` | Authoritative OpenAPI 3.1 and runtime component-schema export. |
| `wire.py` | Source-derived durable record models and typed HTTP response envelopes. |
| `workflows.py` | Read-only registered workflow definitions for transition-graph clients. |
| `workspaces.py` | Workspace catalog, summary and overview snapshots, and local data composition. |
| `state.py` | Workspace evidence, lineage, and consent/audit record projection. |
| `artifacts.py` | Durable artifact catalog, strategy-sketch revision inspection and bounded UTF-8 file reads. |
| `strategies.py` | Workspace strategy registry projection and strategy-domain endpoints. |
| `backtests.py` | Backtest records and result views, with launch delegated to the harness adapter. |
| `deployments.py` | Deployment, order, portfolio, and risk-decision endpoints. |
| `dashboards.py` | Workspace-persisted dashboard widget endpoints. |
| `screeners.py` | Workspace screener definitions and harness-backed screening endpoints. |
| `harness.py` | Harness MCP-backed UI operations and imported artifact serving. |
| `knowledge.py` | Knowledge-library search endpoints. |

## Timeline Boundary

`GET /v1/workspaces/{ws}/chats/{chat}/timeline` provides paginated REST history.
Its initial `after` watermark anchors the sole conversation SSE route,
`GET /v1/workspaces/{ws}/chats/{chat}/timeline/stream`. The stream tails greater
sequences and resumes with `Last-Event-ID`; thread filtering narrows the same
timeline rather than introducing another delivery path.

Legacy execution, chat-message, and sub-agent SSE routes are removed. REST
execution `/executions/{execution_id}/events` and chat-scoped sub-agent
`/sub_agents/{sub_agent_id}/events` endpoints remain for event replay.
`streaming.py` contains transport primitives, not separate stream routers.
The frontend retains only `TimelineStream` and derives execution-indexed live
cards from this shared timeline, not a singleton execution or per-card stream.
Execution snapshots represent first-save state and may be projected late. The
frontend replays all available lifecycle events against them rather than treating
their sequence as a current-state cutoff. Phase navigation selects execution ID
and canonical phase without adding an HTTP stream.

Workflow definitions remain read-only. The frontend shares versioned topology
reads through a bounded 64-entry cache; this is not an HTTP execution-state
cache or another event delivery path. Visibility-based canvas animation pauses
are frontend presentation behavior and do not pause runtime execution.

## Neighbors

Chat and conversation services own durable intake and admission; workflow
services own phase policy; workspace and harness adapters own their product
operations. `schema.py` and `wire.py` supply the backend contract consumed by
`src/ui/backend-wire.json` and the frontend generator. Missing composed services
fail closed rather than creating request-local defaults. Serving requires
`WEB_CONCURRENCY=1` for process-local scheduling and brokers.

The message schema preserves null metadata. Object metadata independently
validates canonical session messages and aggregate datums; intersecting those
object extensions with the null branch would incorrectly exclude a serialized
null value. Neutral delivered datums retain their own schema and do not become
model-requested tool outcomes.
