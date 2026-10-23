# UI behavior verification

These tests render real components and exercise transport, transcript, observation
and lifecycle boundaries. Browser-only user flows live in `src/ui/e2e/`; financial
statistics remain backend authority. External APIs are faked rather than executed.

- `app-navigation.ts` supplies the navigation boundary for component tests.
- `app-state.ts` supplies route state for component tests.
- `artifacts.test.ts` verifies artifact discovery and file inspection.
- `backend-kind.test.ts` verifies backend selection and isolation.
- `backtest-statistics.test.ts` verifies published statistics and undefined reasons.
- `capability-surface.test.ts` verifies capability-driven rendered controls.
- `chat-host-render.test.ts` verifies the actual page host and transport integration.
- `chat-host.test.ts` verifies workspace chat lifecycle and history.
- `chat-scroll.test.ts` verifies transcript scrolling behavior.
- `harness-execution-card.test.ts` verifies live execution state, phase progress,
  long-running backtest observation, transient read recovery and navigation cleanup.
- `job-identity.test.ts` verifies one execution identity across API projections.
- `model-output.test.ts` verifies structured model-output display.
- `multi-workflow.test.ts` verifies independent workflow timelines and scale.
- `node-send-conflict.test.ts` verifies node input and conflict recovery.
- `phase-attempts.test.ts` verifies phase-attempt selection.
- `phase-run-attempts.test.ts` verifies rendered phase execution attempts.
- `research-contracts.test.ts` verifies untrusted research evidence decoding.
- `setup.ts` supplies browser capability stubs and rendering cleanup.
- `sse-client.test.ts` verifies stream framing, reconnect and cancellation.
- `subagent-reentry.test.ts` verifies child conversation reentry.
- `timeline-controller.test.ts` verifies ordered timeline acquisition and updates.
- `timeline-fixture.ts` builds deterministic timeline fixtures.
- `variation-report.test.ts` verifies research comparisons and linked evidence.
- `wire-contract.test.ts` verifies authoritative wire schemas and decode failures.
- `wire-generation.test.ts` verifies committed generated contracts.
- `workflow-card-performance.test.ts` verifies shared reads and idle rendering.
- `workflow-card.test.ts` verifies workflow card interactions.
- `workflow-graph.test.ts` verifies workflow graph projections.
- `workflow-live-render.test.ts` verifies live workflow rendering.
- `workspace-context.test.ts` verifies route workspace isolation.
- `workspace-switcher.test.ts` verifies workspace selection.

`backend-message.json`, `backend-thread.json` and `backend-tree.json` are canonical
wire fixtures. `fixtures/` holds bounded evidence and UI scenarios.

Timeline pagination tests now exercise one page per navigation action, selected
thread filters and cursor-preserving retries. Browser behavior is verified in
`../../e2e/history-loading.spec.ts`.
