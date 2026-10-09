# Thesis UI Design Specification

Status: accepted design with the implemented chat timeline and scope boundaries
described below; the broader page requirements remain the design baseline.

The artifact gallery includes committed strategy sketches with representation
graphs, assumption investigations, pinned components and revision history.
Workspace exploratory-output pages read canonical job artifacts through bounded
backend projections, displaying sequence values and records with timing and
undefined states. Screener authoring accepts multiple declared bar intervals;
generated filters use the primary interval and custom SDK source can compose
all declared intervals. UI projections do not reconstruct indicator histories.

Both local and production API entrypoints explicitly compose the harness data
service, independently of whether a model provider is enabled. Job inspection,
exploratory output pages and exports use that service's resolved MCP topology.

This document is the design specification for the Thesis web UI. It captures
the product requirements, the adopted information architecture, the visual
design language, the backend contracts the UI must preserve, and the
explicit deferrals. Implemented details are distinguished from the broader
product requirements; this document does not extend backend contracts.

## Implemented Chat Boundary

`ChatHostController` in `chat-host.svelte.ts` owns conversation listing,
selection, drafts, create/post/delete, percolation receipts,
and attachment/request cancellation for both the full chat and copilot.
`chat-navigation.ts` owns the URL adapter and clears scope/hash when changing
conversation. Hosts supply navigation and presentation, not separate lifecycles.

The workspace layout gates rendering on one lightweight workspace-summary
request (`GET /v1/workspaces/{id}`) used for existence and the workspace name.
Counts, activity, open chats, strategies, widgets, and the workspace catalog are
requested after render and update the shell when they arrive; they do not block
the first paint.
Prompt suggestions belong only to the unpersisted draft surface, never to an
existing chat with empty or pending history. Initial list loading is not a draft.

`src/ui/src/lib/timeline.svelte.ts` owns the chat's `TimelineController`: one
live timeline subscription, backward history loading, ID deduplication, ascending
`seq` order, and derived thread indexes. Root and scoped transcripts select from
that same owner. History preserves partial pages on failure, checks cursor
progress, and rejects aborted or previous-chat replies. Scope navigation does
not create another message/event cache or an execution SSE subscription.
The first history page fetches all root-owned threads and returns an `after`
watermark. SSE starts only afterward and reads strictly greater sequences, with
`Last-Event-ID` reconnect resume; it never independently backfills old history.
Fallback execution cards wait for that initial page. Older pages remain
progressive, and a history failure exposes retry without opening an unanchored
stream. Conversation and node read routes run synchronous
storage work off the HTTP event loop. Timeline projection repair is removed;
the workspace catalog is the authoritative timeline and streams wake on the
in-process post-commit notification; a recovery-interval fallback bounds a lost
wake.

`workflow-live.ts` derives an execution-indexed `Map<string, WorkflowLiveState>`
from workflow anchors, timeline execution snapshots, and events grouped by
execution ID. The singleton `effectiveExecution` is removed. Each live workflow
card selects its own execution ID, so concurrent executions keep independent
state, phases, and attention indicators. Execution snapshots record first-save
state and may arrive late: the projector replays all available lifecycle events,
including events preceding the snapshot's timeline sequence. Durable node
projection enriches the map without another stream; only declared workflow
phases and explicit phase executors own phase steps, not helper agents.
Scope belongs to the conversation, not the most recently launched workflow.
`sse.ts` retains only the `TimelineStream` client and its framing, URL, and cursor
helpers.

Conversation-owned records carry `conversation_id`, `root_conversation_id`,
`parent_conversation_id`, and authorization `workspace_id`. `chat_id` remains the
persisted root identity. Catalog queries filter ownership on indexed top-level
fields before hydration; there is no legacy ownership predicate. Deletion
commits a retained root tombstone, hides the tree immediately, fences writes,
and queues bounded catalog/timeline cleanup on one background worker. Published
workspace artifacts and execution-store phase results are not conversation data
and are retained. Startup resumes incomplete cleanup from durable tombstones.

List, history-open, and logical-delete latency MUST be below one second against
real persistence during concurrent conversation writes. The opt-in executable
acceptance is `tests/thesis/cosmos/test_conversation_latency_live.py`; it includes
nested agents, tool records, invocation cleanup, and physical timeline cleanup.
It measures in-process HTTP over Cosmos, not browser boot or remote deployment
network overhead; those require separate live browser measurements.

`WorkflowScopeController` in `workflow-scope.svelte.ts` owns view-only navigation,
expansion, durable node capabilities, and operator submission. It requests
history from the injected timeline and derives the tree from durable events and
listed nodes. It neither executes workflows nor invents phase-agent topology.
Workflow-node implementation records are hidden and their children navigate via
the nearest visible ancestor. Only an input-capable agent exposes a composer;
backend anchored admission remains authoritative.
Phase navigation and event selection are keyed by execution ID plus canonical
phase name, so identically named phases in concurrent workflows cannot collide.
Ambiguous phase-agent matches require an explicit agent link; a phase without
an agent opens a read-only event view.

The full chat and copilot share host, timeline, navigation, and transcript/datum projection. A tool call may
produce multiple typed datums in ONE canonical incoming receipt with an ordered
`datums` array; pure shared projection expands each at its caller's anchor, with
a generic fallback. Cards do not persist duplicate tool results or deliver input.
Composers remain writable during an
agent turn; only a message POST in flight blocks duplicate submission. Failed
sends retain the draft and show an inline error.

Re-entry reuses a durable phase node without making its prior terminal state
permanent. Attempt identity and record timestamps reconcile snapshots with
sequence-ordered events; stale history or REST responses cannot close a newer
attempt or revoke its current input capability. Scope attachment generations
also reject responses from a previous attachment.

`WorkflowCard` requests `GET /v1/workflows/{workflow_id}?version=...` through
`api.ts`'s shared topology cache. The cache is bounded to 64 entries, including
tracked in-flight reads, and keys backend, API base URL, workspace, workflow ID,
and recorded version. Matching explicit-version definitions expire after five
minutes; failures, mismatches, and unversioned latest reads are not retained
after completion. It shares topology, not mutable execution state.
The card renders declared transitions only when that definition matches
the execution's recorded version. A missing or mismatched definition shows
topology unavailable, retaining observed activity rather than inventing edges
or claiming the endpoint serves historical versions.
Canvas `requestAnimationFrame` animation runs only for active or attention
nodes while the card intersects the viewport and the document is visible.
Offscreen cards and hidden tabs pause the animation loop; visibility changes
resume it without another subscription.

### Generated Wire Boundary

The Option 1 backend modules now provide one neutral session inbox and
one canonical incoming writer for root, helper, and phase inputs, including tool
results and asynchronous outcomes. Different composers, endpoints, cards, and
provenance labels MAY remain; they MUST NOT become independent persistence or
delivery paths. UI presentation/source metadata MUST NOT grant execution authority
or select privileged provider roles. Receipts MUST distinguish durable admission
from model-boundary consumption; rendering or scheduling is not a consumption
ACK. Consumption follows a successful request snapshot and persisted output;
late mid-model input and tool results after a terminal request remain unconsumed.
Provider protocol grouping MUST NOT change the canonical timeline's arrival
order. `Conversations.deliver_session_message` and handle delegation converge on
`runtime/incoming.py::write_incoming`; presentation is not another delivery path.
Neutral directions are `incoming`, `outgoing`, and trusted-runtime-only
`instruction`. A durable record without canonical session data is not decoded
and cannot become bootstrap authority. Provider-native fields in opaque
invocation audits may
be inspected, never treated as session types or reconstructed by UI role mapping.
Decoding is canonical-only, so no historical or old-delivery branch exists to
compete with the frontend view. Steering effects/intent epoch precede the
canonical ready wake; a rendered admission receipt does not certify consumption.
See runtime `REQ-DEL` and
`../instructions/agent-session-delivery.instructions.md` for the required
executable verification; documentation does not prove full compliance.

`thesis.http.schema` exports OpenAPI 3.1 with the source-derived runtime
tree/message/activity/datum/tool schemas. Durable record fields come from backend
dataclasses; response envelopes and complete node-input receipts come from typed
backend models. `src/ui/backend-wire.json` is that backend export, not a frontend
field inventory. `generate-wire.mjs` emits `src/lib/generated.ts` and
`src/lib/wire-schema.json`; its `--check` mode compares both artifacts byte for
byte. Generation and runtime receipt schemas are implemented.

`wire.ts` owns one cached AJV 2020-12 decoder registry. Endpoint validators in
`api.ts` decode thread lists/create records, messages, executions, workflow
definitions, tree wrappers/nodes, steering, timeline pages, and full node-input
receipts before UI adaptation. SSE frames use the same decoder, including nested
tool outcomes in event extension payloads. Runtime activity is discriminated by
`activityKind`; historical untagged metadata is allowed by the backend contract,
not independently guessed by a competing UI schema. Durable tree projections
retain completion mode, return anchor, phase authority, tools, budgets, contracts,
attempts, results, and timestamps; context is not invented from a durable node.

`transport.ts` handles HTTP mechanics and exposes `ThesisApiError` with status,
path, code, message, and details. The canonical envelope is
`{error: {code, message, details?}}`; backend translation distinguishes missing
records, cross-workspace/lifecycle conflicts, validation, unavailable services,
MCP authentication/transport/protocol/tool errors, and sanitized internal faults.
An arbitrary HTTP failure is not an empty successful result; only an explicitly
optional resource may use its endpoint's absence fallback. Invalid canonical
error envelopes report `invalid_error_response`; malformed success JSON or wire
records fail decoding rather than silently accepting arbitrary payloads.

`fixture-timeline.ts` supplies offline pages and subscriptions to the same
timeline controller. Seeds and mutations validate the same complete wire shape
as HTTP/SSE. It does not simulate agent execution or backend input admission;
sample fixture hashes are not claims of backend-issued provenance.

Both `/v1` and `/api/v1` register on the same backend app and resolve the same
app-state services and authentication. Missing production services return 503,
not request-local defaults. Process-local scheduling/brokers require one serving
worker (`WEB_CONCURRENCY=1`). The current hypothesis-development graph is version
`3.0.0`, with deterministic falsification trials, a typed variation report path
that renders per-parameter value versus total-return/profit-factor charts, and
executor-free terminal nodes.

<!-- markdownlint-disable MD013 -->

## Decision Record

- **Stack: Svelte (SvelteKit, Svelte 5 runes, TypeScript, Tailwind CSS v4,
  adapter-static SPA).** Product decision, overriding the roadmap's earlier
  "React" wording and the reference mockups' React implementations. The
  deciding factors: the repository's existing `src/ui` is SvelteKit with
  production-grade chart/report components (T210's `TimeSeriesChart`,
  `HistogramChart`, `BacktestReport`, `StatsTable` — cursor legend, OHLC
  readout, train/test report composition patterns) that the workspace UI
  reuses and extends directly; Svelte 5 runes give the streaming/event-heavy
  state model a smaller surface than an equivalent React stack; and one stack
  across the demo laboratory and the product eliminates translation work.
  The mockups remain the sources for information architecture and visual
  design (both are stack-neutral at that level). The Svelte app talks to the
  Thesis API as an SPA (`adapter-static`), reusing `src/ui`'s established
  SvelteKit/Tailwind conventions.
- **Structure source: the workspace-app mockup** ("svelte-trading-workspace-ui",
  internally Next.js). Its information architecture is adopted: persistent
  sidebar navigation, workspace switcher, collapsible copilot panel, command
  palette, and the Overview / AI chats / Strategies / Backtests / Deployments /
  Dashboards page set. It is extended with two pages this specification adds:
  **Screener** (Rust-authored, executed by the harness) and **Workspace
  state**.
- **Visual source: the workspace-app mockup** ("svelte-trading-workspace-ui").
  Product decision 2026-09-09: the UI adopts the mockup's **light sage/cream
  design language across the board** — white cards on an off-white canvas,
  visible sage-gray borders, muted sage-green accents
  (`--color-accent: #2a8761`, profit `#2a8761`, loss `#c76868`, warn
  `#d6ad58`), Inter at compact sizes with dark sage numerics, solid white
  card surfaces, and focus rings in the sage family. This replaces the
  earlier dark navy/cyan reading taken from the backtest mockup; the dark
  utility classes (`text-slate-*`, `border-white/*`, translucent surface
  variants) are remapped to the light tokens globally so existing markup
  renders with correct light-theme contrast. The backtest mockup's data
  composition (metric cards, split equity, trades table, risk panel) is
  retained as layout guidance only.
- **`src/ui` disposition:** the existing SvelteKit app is promoted from
  demo playground to the seed of the production UI. Its `demo/*` routes and
  component laboratory remain during early implementation and retire as the
  real workspace pages absorb the components.

## Product Requirements (source of truth)

1. **Workspace selection** — the user picks among workspaces they can access.
2. **Workspace as home** — a workspace contains its chats with the LLM bot,
   the code of its trading strategies, deployment status and monitoring, and
   the workspace's evidence, strategy lineage, and consent/audit records.
3. **Workspace-scoped chat** — chat with the LLM scoped to the workspace,
   covering strategy modification, backtests, paper deployment, and hypothesis
   discovery with falsification testing. The chat behaves as if the CLI were
   scoped to the workspace.
4. **Backtest results** — inspect results of a workspace's backtests.
5. **Deployment monitoring** — monitor deployed paper/live strategies:
   results, P&L, orders.
6. **Custom widgets/dashboards** — host widgets and dashboards created inside
   the workspace.
7. **Screeners** — screen symbols by volume or advanced programmatic custom
   screeners authored as Rust programs against the harness authoring SDK; the
   filter builder generates the source, execution is delegated to the harness
   MCP, and saved definitions and imported results stay in the workspace with
   bundle-hash provenance.
8. **Train/test split** — backtest results expose the train/test split.

## Information Architecture

### Application shell (from the structure mockup)

- **Left sidebar (persistent):** brand mark; **workspace switcher** popover
  listing accessible workspaces with avatar/initials plus "Create workspace";
  global search trigger (`⌘K` command palette); primary navigation with live
  counts; a **Pinned** section (pinned widgets and a pinned strategy);
  footer with workspace health, help, settings, and the user profile.
- **Top bar:** breadcrumbs (`Workspace / <name> / <page>`), environment badge
  (dev is explicitly badged "Simulation environment"), docs link,
  notifications popover fed by workspace activity, copilot toggle.
- **Copilot panel:** collapsible right-hand assistant on every page except
  the full-page chats view; expand-to-full-chat affordance; workspace scope
  indicator ("In context: <workspace>").
- **Command palette (`⌘K`):** go-to-page, strategies, dashboards, quick
  actions (create strategy / create workspace / new chat / new screener with
  the Rust filter builder).

### Pages

| Page | Purpose | Key elements |
| --- | --- | --- |
| **Overview** | Workspace landing | Recent activity feed, strategy health cards, latest backtests, pinned widgets strip, open chats. |
| **AI chats** | Full-page workspace chat | Conversation list (workspace-scoped, searchable) + transcript pane. Multiple concurrent threads per workspace. |
| **Strategies** | Strategy registry projection | Rebuildable projection over the workspace's Rust strategy artifacts (`strategies/*/*/manifest.json`); the tree is the source of truth, not a store. List/grid toggle, status filters (live/paper/paused/draft), per-strategy code view with lineage (version ancestry), New-strategy flow. |
| **Backtests** | Run history + results | Summary strip (count, best return, average Sharpe), filterable history table, **detail view with train/test split** (below). |
| **Deployments** | Paper/live monitoring | Running/paused cards with health, allocated capital, P&L; orders and fills table with mode badges (paper/live); risk panel. |
| **Dashboards** | Custom widgets | Widget grid, pin/unpin to sidebar, create/remove widgets, widget cards for equity, P&L, allocation, win rate, orders. |
| **Screener** | Symbol screening | Volume/liquidity filters plus custom screeners authored as Rust programs (the filter builder generates SDK source and compiler errors render inline); definitions saved as content-addressed workspace artifacts; imported results carry bundle-hash provenance. |
| **Workspace artifacts** | Durable workspace records | Gallery and explorer over committed strategies, studies, and indicators: file tree with a bounded UTF-8 viewer, declared ingestion, version/revision history, and type-specific previews (strategy ingestion + exit criteria, study window/indicators/metrics, indicator consumers). Volatile stores are excluded. |

### Requirement mapping

- **R1 (workspace selection):** workspace switcher + create-workspace dialog;
  selection persists client-side and re-scopes every view. Authorization is
  the backend's: the UI renders only what the API returns for the caller.
- **R2 (workspace as home):** the sidebar navigation set plus the Workspace
  artifacts page; every page is workspace-scoped by the route
  (`/w/{workspace}/…`).
- **R3 (workspace chat):** full-page chats view + side copilot; message
  composer with strategy-context attachment; chat drives operations and
  navigates (a suggested-action affordance deep-links from chat into
  strategy/backtest/deploy views).
- **R4/R8 (backtest results + split):** backtest detail page renders the
  equity curve segmented **in-sample / out-of-sample** at the manifest's
  declared split boundary: split marker on the curve, per-segment metric
  cards, drawdown per segment, and an explicit "not declared" state when the
  manifest carries no split (never fabricated).
- **R5 (deployment monitoring):** Deployments page: deployment cards
  (health, mode, allocated capital, live P&L), orders/fills table, risk
  decisions panel, and per-deployment drill-down.
- **R6 (widgets):** Dashboards page; widget definitions are **workspace
  artifacts** (created by the agent or the user, stored in the workspace Git
  repository). The UI renders declared widget types plus sandboxed custom
  widgets; definitions are fetched, never authored client-side as state.
- **R7 (screener):** Screener page: volume/liquidity preset filters, custom
  screener authoring through the Rust SDK/JIT seam (the filter builder
  generates source; compiler errors render inline), saved content-addressed
  definitions listed and re-runnable, and imported results with bundle-hash
  provenance; execution is delegated to the harness MCP. Results link into
  strategies.

### Chats view detail (investigations, uploads, timelines, steering, evidence)

- **Multiple chats and investigations:** each workspace holds many
  conversation threads; a thread may be typed as an ordinary operation chat
  or an **investigation** (a hypothesis-discovery thread with its candidate
  explanations, falsification tests, and verdict trail surfaced inline).
- **Uploads:** the composer accepts file uploads that the API stores as
  **workspace-scoped untrusted evidence** (content hash + provenance); the
  transcript renders uploaded evidence with its trust tier and never treats
  embedded instructions as platform instructions.
- **Workflow timelines:** each chat exposes all workflows it drove through one
  timeline stream. Machine events (phases, tool submissions/results, steering
  acknowledgements) update live cards by execution ID, with the SSE cursor state
  visible on reconnect.
- **Steering:** steering is a message; when an execution is active, posting
  a message shows the percolation acknowledgement (intent epoch, durable
  ack) inline in the transcript.
- **Evidence:** claims and results in the transcript link to their evidence
  (backtest run, artifact digest, knowledge page citation with Git revision);
  evidence badges deep-link into Backtests/Dashboards.
- **Knowledge search:** the copilot composer supports a knowledge-search
  affordance (global Markdown corpus with trust tiers, path/revision
  citations); results are quoted with provenance and injected as context
  the user can inspect.
- **Strategy lineage:** strategy references in chat and in the Strategies
  page expose lineage: version ancestry, parent adaptation, and the
  experiment/exposure history summary.
- **Activation consent:** when the workflow reaches an activation gate, the
  transcript renders the grant proposal (bundle hash, environment, action,
  bounds, expiry, scope) and captures the explicit structured confirmation
  inline — mirroring the API's T172 contract; no free-text confirmation
  exists.

## Backend Contracts the UI Must Preserve

- **Chat and steering:** send messages via
  `POST /v1/workspaces/{ws}/chats/{chat}/messages`; steering is a chat
  message, never an execution-scoped call. The UI never calls the deprecated
  execution steer alias.
- **Streaming:** the implemented chat surface consumes `GET
  /v1/workspaces/{ws}/chats/{chat}/timeline/stream` (SSE), merging durable messages
  and events plus execution snapshots by timeline ID and `seq`. The stream wakes
  on the in-process post-commit notification; SSE `id` is the sequence resume cursor
  used with `Last-Event-ID`. Thread-scoped REST history uses the same chat's
  `/timeline?thread=...&before=...` until `nextBefore` is null. Legacy execution,
  chat-message, and sub-agent SSE routes are removed. Execution and sub-agent
  REST `/events` replay endpoints remain; they are not live subscriptions.
  Ephemeral provider deltas are not durable timeline history.
  Assistant and reasoning text render as sanitized Markdown, with LaTeX math
  (`\(...\)` inline, `\[...\]` display) rendered to KaTeX and the result passed
  through the same sanitizer. A bare `$` is never treated as math so currency
  and dollar-denominated numbers render as plain text. Harness tool
   outcomes are persisted as ordered neutral incoming receipts at their durable
   arrival positions; retained chat-role fields are presentation only. A receipt's
   datum array renders through the shared pure typed-datum projection with a generic
  fallback, so any harness MCP tool can supply a custom in-chat card without
  changing the transcript.
- **Artifacts:** content-addressed workspace paths; the UI downloads by
  reference and never reassembles or rewrites artifact content.
- **Authorization:** bearer authentication to the Thesis API
  (`THESIS_API__BEARER_TOKEN` mode today, Entra audience mode supported);
  the UI holds only what it needs for its own session and no broker or
  gatekeeper credentials. Activation status is read-only.
- **Consent:** activation consent is the T172 structured surface: the UI
  renders the grant proposal and captures an explicit structured
  confirmation (re-stated bundle hash, environment, action) — free-text
  confirmation is structurally impossible in the UI the same way it is in
  the API.
- **No second source of truth:** all durable state (strategies, backtests,
  deployments, widget definitions, screeners, chats, and workspace records) lives
  in the workspace repository and is served by the API. Client
   state is view state plus one rebuildable timeline projection (selections,
   filters, transient composer input, and the timeline's ordered items).
  Optimistic updates are bounded to cosmetic acknowledgment and re-synced.

## Visual Design Language (from the styling mockup)

- **Surfaces:** `--color-surface-950: #070b14` page background with faint
  48px cyan grid; cards on `surface-850/800` with `border-white/5`;
  `surface-700/600` for borders and rails.
- **Accent & semantics:** cyan `#22d3ee` (accent, primary buttons, focus
  rings, live indicators), `--color-profit: #34d399`, `--color-loss:
  #f87171`, `--color-warn: #fbbf24`. P&L and performance numerics always use
  profit/loss semantics with signed formatting.
- **Typography:** Inter for UI copy; JetBrains Mono for every numeric field,
  price, hash, and code surface. Tabular numerals in tables.
- **Data presentation:** the backtest mockup's composition is adopted for
  Backtests detail and Deployments: summary strip (initial/final/net/period),
  metric cards (Sharpe, max drawdown, win rate, profit factor), equity chart
  with drawdown companion, monthly-returns heatmap, trades table, trade
  stats, risk panel, run details card. The existing Svelte components
  (`TimeSeriesChart`, `HistogramChart`, `BacktestReport`, `StatsTable`)
  already implement much of this composition and are restyled to the theme
  tokens rather than rewritten.
- **States:** loading progress bar in the top bar; skeleton cards for
  first-load; toasts for action feedback; inline confirmation for
  destructive actions; explicit empty states ("quiet empty" copy style).
- **Charts:** Recharts or visx (implementation task's choice) with the theme
  tokens; consistent axis/tick styling in mono font; drawdown as a red-tinted
  area under the equity line.

## Explicit Deferrals (to the later implementation task)

- Page/component implementation and responsive/mobile layout details.
- Charting library finalization (the existing Svelte chart components are
  the baseline; any additional library must fit Svelte 5 runes).
- Data-loading details for pages outside the implemented chat controller;
  chat history/live ownership and scoped navigation are implemented above.
- Packaging/hosting integration with the T174 deployment (the SvelteKit
  static build served by the Thesis API or a CDN, with the API's auth
  mode) — decided with the first implementation wave.
- Realtime presence, multi-user collaboration, and notification digests.
- Any change to backend routes or contracts (this specification consumes;
  it never extends).


Research variation and forward cards consume backend-produced accepted evidence,
with numeric-string parsing only for chart display. Per-parameter return and
profit-factor plots preserve undefined reasons and failed/missing/untested
states. Declared joint experiments show paired parameter observations; forward
cards match development vectors by canonical identity. Export retains the full
evidence and frozen/launch designs, and job drill-down reads authoritative
harness receipts and artifact references. Reused holdout designs display their
adaptive evidence scope. UI fixtures are generated from Python contract records,
not independently authored financial examples.

Backtest statistics use canonical snake_case fields throughout import and rendering. The UI does not reconstruct aliases or headline financial statistics. Drawdown plot titles use the published maximum and its undefined reason; chart series are visualization only.
