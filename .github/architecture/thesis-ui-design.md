# Thesis UI Design Specification

Status: accepted design for the chat-first Thesis workspace experience.

This document is the design specification for the Thesis web UI. It captures
the product requirements, the adopted information architecture, the visual
design language, the backend contracts the UI must preserve, and the
explicit deferrals. It is a specification only: it deliberately does not
implement the UI and does not modify any backend contract.

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
  **Screener** and **Workspace state**.
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
   the workspace's CLEO state.
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
   screeners; save filters/studies to the workspace.
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
  actions (create strategy / create workspace / new chat / new screener).

### Pages

| Page | Purpose | Key elements |
| --- | --- | --- |
| **Overview** | Workspace landing | Recent activity feed, strategy health cards, latest backtests, pinned widgets strip, open chats. |
| **AI chats** | Full-page workspace chat | Conversation list (workspace-scoped, searchable) + transcript pane. Multiple concurrent threads per workspace. |
| **Strategies** | Strategy registry | List/grid toggle, status filters (live/paper/paused/draft), per-strategy code view with lineage (version ancestry), New-strategy flow. |
| **Backtests** | Run history + results | Summary strip (count, best return, average Sharpe), filterable history table, **detail view with train/test split** (below). |
| **Deployments** | Paper/live monitoring | Running/paused cards with health, allocated capital, P&L; orders and fills table with mode badges (paper/live); risk panel. |
| **Dashboards** | Custom widgets | Widget grid, pin/unpin to sidebar, create/remove widgets, widget cards for equity, P&L, allocation, win rate, orders. |
| **Screener** | Symbol screening | Volume/liquidity filters plus programmatic custom screeners authored in the workspace; save screeners/studies as workspace artifacts. |
| **Workspace state** | CLEO projection | Workspace CLEO state: plan/workflow timelines, evidence index, strategy lineage graph, knowledge search, consent/audit trail. |

### Requirement mapping

- **R1 (workspace selection):** workspace switcher + create-workspace dialog;
  selection persists client-side and re-scopes every view. Authorization is
  the backend's: the UI renders only what the API returns for the caller.
- **R2 (workspace as home):** the sidebar navigation set plus Workspace state
  page; every page is workspace-scoped by the route
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
  programmatic screener authoring (code artifact in the workspace), saved
  screeners/studies listed and re-runnable; results link into strategies.

### Chats view detail (investigations, uploads, timelines, steering, CLEO, evidence)

- **Multiple chats and investigations:** each workspace holds many
  conversation threads; a thread may be typed as an ordinary operation chat
  or an **investigation** (a hypothesis-discovery thread with its candidate
  explanations, falsification tests, and verdict trail surfaced inline).
- **Uploads:** the composer accepts file uploads that the API stores as
  **workspace-scoped untrusted evidence** (content hash + provenance); the
  transcript renders uploaded evidence with its trust tier and never treats
  embedded instructions as platform instructions.
- **Workflow timelines:** each chat exposes the execution timeline of the
  workflow it drove: machine events (phases, tool submissions/results,
  steering acknowledgements) rendered from the durable event stream, with
  the SSE cursor state visible on reconnect.
- **Steering:** steering is a message; when an execution is active, posting
  a message shows the percolation acknowledgement (intent epoch, durable
  ack) inline in the transcript.
- **CLEO plan visibility:** the transcript renders the agent's current plan
  and task list (read-only projection of CLEO state served by the API) —
  plan steps, current stage, and completion gates; the UI never mutates CLEO
  directly.
- **Evidence:** claims and results in the transcript link to their evidence
  (backtest run, artifact digest, knowledge page citation with Git revision);
  evidence badges deep-link into Backtests/Dashboards/Workspace state.
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
- **Streaming:** consume `GET /v1/workspaces/{ws}/executions/{id}/events/stream`
  (SSE). Durable execution events carry the replay cursor: the client stores
  the last `id` and reconnects with `Last-Event-ID`. Ephemeral frames (chat
  message events, `provider.delta`) have no `id` and are deduplicated by
  message ID (or tool call id); they are never replayed after reconnect.
  Assistant and reasoning text render as sanitized Markdown. Harness tool
  outcomes are persisted as ordered `tool`-role chat messages at the position
  they ran and render through a tool-name-to-component registry with a generic
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
  deployments, widget definitions, screeners, chats, CLEO projections) lives
  in the workspace repository/CLEO state and is served by the API. Client
  state is view state only (selections, filters, transient composer input).
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
- Data-loading and state details within the SvelteKit conventions (load
  functions, runes stores) — constrained by: workspace id in the URL, SSE
  cursor persisted per execution subscription, and re-entrancy on reconnect.
- Packaging/hosting integration with the T174 deployment (the SvelteKit
  static build served by the Thesis API or a CDN, with the API's auth
  mode) — decided with the first implementation wave.
- Realtime presence, multi-user collaboration, and notification digests.
- Any change to backend routes or contracts (this specification consumes;
  it never extends).
