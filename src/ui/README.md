# FCA Thesis UI

SvelteKit front-end for the FCA Thesis workspace. This package is the only UI
home in the repository; the earlier root-level `ui/` directory was retired and
no legacy UI files remain tracked in git. Product-level placement of this
package is defined in `.github/architecture/fca-thesis.md`; this README
documents the local slice only.

## Purpose

Provide the interactive Thesis surfaces: workspace conversations, strategy
lineage, evidence and consent/audit views, and canvas-based charting for
backtest reports (PnL, executions, per-instrument prices). Deterministic
fixtures support local demos alongside Thesis backend payloads.

## Architecture and design decisions

- **One conversation timeline.** Full chat and copilot share `ChatHostController`
  and `TimelineController`. REST history establishes a watermark before the sole
  `TimelineStream` SSE client subscribes; root and scoped views select from the
  same ordered items without extra streams.
- **Execution-indexed live cards.** `src/lib/workflow-live.ts` derives a map keyed
  by execution ID from timeline anchors, snapshots, events, and durable nodes.
  Each card selects its own live state; the singleton `effectiveExecution` is
  removed. Legacy execution, chat-message,
  and sub-agent SSE routes are removed; REST event replay endpoints remain.
- **Snapshot and phase projection.** First-save execution snapshots may arrive
  late, so all available lifecycle events replay against them, not only events
  after their sequence. Declared phases and explicit phase executors own phase
  steps; helpers do not. Phase navigation is keyed by execution ID and canonical
  phase name to keep concurrent workflows separate.
- **Bounded topology and rendering.** `src/lib/api.ts` shares workflow-definition
  reads in a 64-entry cache, including tracked in-flight requests, keyed by
  backend, API URL, workspace, workflow ID, and version. Matching explicit-version
  definitions expire after five minutes; failures, mismatches, and unversioned
  reads are not retained after completion. Workflow-card canvas animation uses
  `requestAnimationFrame` only for active/attention nodes while visible, pausing
  offscreen and in hidden tabs without pausing runtime execution.
- **Two-layer chart design.** `TimeSeriesChart` is the base chart component:
  one instance renders one dataset (or a small named series list on a shared
  time axis) and owns all interaction — pan, wheel time-zoom, shift+wheel
  value stretch, overview-strip grip handles, snapped crosshair readout, and
  series rendering (candlesticks by default, line mode with optional gradient
  area fill for curves). It holds no backtest knowledge.
- **Shared zoom camera.** The base chart exposes its viewport as a bindable
  `camera` prop shaped `{ t0, t1 }` (the visible time window). Compositions
  bind every chart instance to one page- or report-owned camera, so pan/zoom
  on any panel synchronizes the time axis across all panels while each panel
  keeps its own value scaling: the value axis always auto-fits the visible
  window, and shift+wheel applies a per-chart local value zoom that is not
  shared with other panels. Double-click resets time window and value zoom.
- **Composite report card.** `BacktestReport` is the chat report element: a collapsible card whose
  collapsed state shows a headline stat strip plus a PnL preview chart with fill markers, and whose
  expanded state (click the header) shows tabs. Tabs are derived from the report payload in fixed
  order: PnL & orders (equity curve with entry/exit markers plus the fills table), Securities (one
  candlestick panel per traded instrument, underlying labeled in the title), Risk analysis (risk
  tiles, filled equity area, drawdown area with max-DD badge, and a trade PnL distribution
  histogram — all derived client-side from equity/trades), one tab per model-provided
  `report.panels` group (generic chart specs fed straight to `TimeSeriesChart`), and Statistics
  last. It only formats statistics supplied by the caller (`report.stats`: the full
  backend metrics dict, e.g. `totalReturn`, `cagr`, `maxDrawdown`, `sharpe`, `sortino`, trade and
  execution aggregates as decimal fractions/ratios); headline tiles prefer known `stats` keys and
  fall back to trivial client-side derivation when `stats` is absent. `StatsTable` renders the
  stats dict as grouped key/value tables (known keys get labels, grouping, and formatting; unknown
  keys fall through to an "Other" group), so new backend metrics appear without UI changes.
- **Theme.** Dark navy trading-desk palette defined in `app.css` with an opaque
  surface ladder: page background (`--bg`), raised card surface (`--panel`,
  with a soft drop shadow for lift), and sunken wells for sub-cards
  (`--surface-well` — chart figures, stat tiles, stats groups, tables), so
  every card header and its body share one flat color. Cyan accent,
  emerald/rose profit/loss semantics, hairline slate borders, Inter/system
  sans with mono numerics (`--font-mono`, JetBrains Mono/Cascadia fallbacks),
  and 12px card radius. Chart components mirror the palette constants so
  canvas rendering matches the CSS tokens.
- **Embeddability.** Components under `src/lib/components/` must not import
  router, page, or network modules. `BacktestReport` is mountable standalone
  on a route and embeddable in a chat context; theming flows through the CSS
  custom properties defined in `src/app.css`.
- **Fixture-driven demos.** Demo routes render entirely from deterministic
  seeded fixtures with no backend. The backtest fixture carries placeholder
  `stats` shaped like the future T207 artifact-view payload.
- **Static deployment.** `@sveltejs/adapter-static` with prerendering enabled
  in `src/routes/+layout.js`; the build produces a fully static site in
  `build/` (git-ignored).

## What this folder must not contain

- Backend execution or admission policy. HTTP and SSE transport belong in
  `src/lib/`; presentation receives decoded data through props and shared context.
- Router/page dependencies inside `src/lib/components/` components.
- Metric or strategy computation — the UI only renders and formats.
- Any Python or harness code; those live in sibling `src/` packages.

## Neighbors

- Serves the Thesis product (`src/thesis/`), which will supply real report
  payloads through the T207 artifact-view endpoint, and the workspace chat
  surface, which embeds the report component.
- Depends on nothing in the repository besides its npm dependencies.

## Layout and files

| Path | Purpose |
| --- | --- |
| `package.json` | Package manifest (`fca-thesis-ui`): dev/build/preview scripts and SvelteKit/Svelte 5/Vite dependencies. |
| `package-lock.json` | Lockfile for the npm dependencies. |
| `svelte.config.js` | SvelteKit configuration; static adapter with an `index.html` fallback. |
| `vite.config.js` | Vite configuration; wires the SvelteKit plugin. |
| `generate-wire.mjs` | Deterministic type/schema generation from committed backend OpenAPI 3.1 and explicit JSON Schema exports; `--check` detects stale artifacts. |
| `backend-wire.json` | Authoritative backend-generated OpenAPI 3.1 export consumed by the frontend generator. |
| `vitest.config.ts` | Frontend test configuration and SvelteKit navigation test adapters. |
| `src/lib/` | Transport, generated wire decoding, shared chat/timeline controllers, execution-indexed workflow projection, and presentation; see its local README. |
| `.gitignore` | Ignores `node_modules/`, `.svelte-kit/`, build output, and env files. |
| `src/app.html` | HTML shell for the SvelteKit app. |
| `src/app.css` | Global dark theme: CSS custom properties (`--panel`, `--border`, `--text`, `--muted`, `--accent`, `--green`, `--red`, ...), base typography, and shared `.page`/`.card` layout helpers. |
| `src/lib/components/charts/TimeSeriesChart.svelte` | Base single-dataset canvas chart: bindable time camera, pan/zoom grip handles and overview strip, wheel time-zoom, per-chart shift+wheel value stretch, auto-fit vertical scaling, snapped crosshair with axis pills and cursor legend, fill markers with tooltips, candle (default) and line render modes with optional gradient area fill, uppercase card header with accent tick, double-click reset. |
| `src/lib/components/charts/HistogramChart.svelte` | Small canvas bar histogram with zero-crossing buckets and green/red trade-outcome coloring, hover bucket tooltip; used for the trade PnL distribution in the Risk analysis tab. |
| `src/lib/components/backtest-view/BacktestReport.svelte` | Collapsible chat report card: header (title, headline stat tiles, expand control) with a PnL preview chart when collapsed and a tabbed body when expanded — PnL & orders (filled equity curve with entry/exit markers plus fills table), per-instrument candlestick panels, Risk analysis (risk tiles, equity and drawdown areas, PnL histogram), model-provided `report.panels` groups as extra tabs, and a Statistics tab; all `TimeSeriesChart` instances bound to one shared camera. No router or page dependencies. |
| `src/lib/components/backtest-view/StatsTable.svelte` | Generic grouped key/value stats table: renders the backend `report.stats` metrics dict with a known-key registry (label, group, percent/ratio/money/ms/duration/bool formatting); unknown keys fall into an "Other" group. No computation beyond formatting. |
| `src/lib/data/backtestFixture.js` | Deterministic seeded backtest fixture: OHLC daily bars for two instruments, consistent equity curve and trades, full placeholder stats dict shaped like the T207 artifact-view payload, and model-provided risk/benchmark panels. |
| `src/routes/+layout.js` | Enables prerendering for all routes. |
| `src/routes/+layout.svelte` | Root layout; imports `app.css` and renders the page. |
| `src/routes/+page.svelte` | Workbench index linking the demo routes with fixture-derived summaries. |
| `src/routes/demo/backtest/+page.svelte` | Backtest visualizer demo: renders `BacktestReport` with the fixture and a page-owned camera bound into the report. |

## Verification

Focused verification for this slice: `npm run build` from this directory must
succeed and produce the prerendered backtest demo page
(`build/demo/backtest.html`) without any backend.
