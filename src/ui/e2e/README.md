# Browser behavior checks

Playwright flows against the running local UI. Live conversation/workflow tests
use the local stack; demo tests use local fixtures and submit no jobs. Browser
configuration belongs in the parent `playwright.config.ts`.

- `fixtures.ts`: local-backend setup, chat creation, workflow navigation and
  conversational operator helpers, and best-effort execution cleanup.
- `read-fixtures.ts`: current display-page and flat-node producers for small scripted HTTP fixtures.
- `conversation-reads.spec.ts`: preview expansion, lazy tool-evidence hydration, immutable detail identity, no-op reselection and cached outline cursor reuse across conversation switches.
- `chat.spec.ts`: immediate pending-message/composer behavior with delayed HTTP acknowledgement, and durable assistant responses.
- `conversation-navigation.spec.ts`: seamless conversation layout, persisted compact navigation with accessible shell actions, collapsible conversation selection retaining drafts, mobile picker behavior, compact request-grouped outline, pointer/keyboard rail resizing, separate workflow/conversation expansion and name navigation, unloaded nested artifact reveals, exact parent breadcrumbs, per-scope reading restoration, and root/child bottom-follow behavior with an arrow-only control hidden at the bottom.
- `workspace-deletion.spec.ts`: current-workspace and picker deletion, name confirmation,
  cancellation, failed-request retry, conversation access removal, and refresh without stale selection.
- `chat-tools.spec.ts`: activity rail disclosures and compact tool presentations.
- `research.spec.ts`: SDK exhibit tabs and evidence interactions across families.
- `study-result.spec.ts`: shared production study lifecycle, plots, persistent heatmap slices,
  semantic raw controls, source inspection, bounded previews, and mobile containment.
- `hypothesis-workflow.spec.ts`: operator journey through hypothesis development.
- `workflow-junction.spec.ts`: shared full-review dots, separate destination subsets, single-target routes, self-loops, hidden terminal-state nodes, straight level arrows, continuous tangents and Bézier bends throughout vertical transitions, shafts ending at arrowhead bases, compact returns from right outputs to left inputs, upward returns without downward detours or source-column overshoot, synchronized pan/zoom, and phase navigation. The strategy RCA/optimization scenario reads the running API's current hypothesis-development topology and scripts only execution activity.
- `workflow-hierarchy.spec.ts`: nested workflow phase navigation without standalone
  phase-agent cards in the launching transcript, with ordinary helpers preserved
  through back navigation and reload; only external HTTP responses are scripted.
- `debug-poll.cjs`: manual live workflow diagnostic script; not a fixture-only test.
- `phases/`: focused operator journeys for individual workflow phases.

These checks assert rendered user behavior, not internal component call order.

- `thinking-stream.spec.ts`: Reasoning growth, collapsed disclosure, refresh reconstruction, stable completion identity and expansion retention through the real chat surface with scripted external timeline responses.

- `history-loading.spec.ts`: rendered recent root history, automatic top paging with visible-message anchoring,
  and selected child history against scripted external HTTP responses; no model
  calls, workspace mutations, or child transcript prefetch.
