# Browser behavior checks

Playwright flows against the running local UI. Live conversation/workflow tests
use the local stack; demo tests use local fixtures and submit no jobs. Browser
configuration belongs in the parent `playwright.config.ts`.

- `fixtures.ts`: local-backend setup, chat creation, workflow navigation and
  conversational operator helpers, and best-effort execution cleanup.
- `chat.spec.ts`: immediate pending-message/composer behavior with delayed HTTP acknowledgement, and durable assistant responses.
- `workspace-deletion.spec.ts`: current-workspace and picker deletion, name confirmation,
  cancellation, failed-request retry, conversation access removal, and refresh without stale selection.
- `chat-tools.spec.ts`: activity rail disclosures and compact tool presentations.
- `research.spec.ts`: SDK exhibit tabs and evidence interactions across families.
- `study-result.spec.ts`: shared production study lifecycle, plots, persistent heatmap slices,
  semantic raw controls, source inspection, bounded previews, and mobile containment.
- `hypothesis-workflow.spec.ts`: operator journey through hypothesis development.
- `workflow-hierarchy.spec.ts`: nested workflow phase navigation without standalone
  phase-agent cards in the launching transcript, with ordinary helpers preserved
  through back navigation and reload; only external HTTP responses are scripted.
- `debug-poll.cjs`: manual live workflow diagnostic script; not a fixture-only test.
- `phases/`: focused operator journeys for individual workflow phases.

These checks assert rendered user behavior, not internal component call order.

- `thinking-stream.spec.ts`: Reasoning growth, collapsed disclosure, refresh reconstruction, stable completion identity and expansion retention through the real chat surface with scripted external timeline responses.
