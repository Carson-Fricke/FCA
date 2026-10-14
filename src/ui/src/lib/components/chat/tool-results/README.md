# Render tool outcomes

These cards consume canonical tool outcomes and shared live projections. They
never dispatch executions or reconstruct financial statistics. Parent chat
components own timeline selection; job observation uses the shared read-only poller.

- `ArtifactsToolCard.svelte`: renders stored artifact manifests.
- `BacktestToolCard.svelte`: renders backtest outcome details.
- `GenericToolCard.svelte`: renders unrecognized structured tool results.
- `JobToolCard.svelte`: renders job lookup outcomes.
- `OperatorQuestionCard.svelte`: renders an agent's conversational input request.
- `RunToolCard.svelte`: renders admitted run outcomes and shared job progress.
- `StudyToolCard.svelte`: one execution header and inline research surface for
  study runs/reads, with compact revision/validation receipts. Completion reveals
  research tabs in place; failed/cancelled runs retain published evidence. Returned
  data includes searchable metrics and the complete retained tool receipt.
- `ReferenceToolCard.svelte`: knowledge and SDK content visible on receipt expansion, with provenance, bounded-content notices, and the complete returned payload.
- `SketchToolCard.svelte`: mechanism, decisions, representations, assumptions,
  evidence links and changes from the previously displayed sketch revision.
  A compact premise and decision summary precede expandable full sections.
- `SubAgentHandleCard.svelte`: renders agent handles with their live scope view.
- `WorkflowExecutionCard.svelte`: renders workflow handles with live phases and admitted job IDs.
- `WorkflowOutcomeCard.svelte`: renders workflow terminal outcomes.
- `ActivityMarker.svelte`: shared book, search, list and fallback dot rail markers.
- `headers.ts`: header adapters deriving actions, subjects and truthful returned counts from structured results and arguments.
- `registry.ts`: registers datum/tool result components and their optional header adapters
  and applies the collapsed-read default, with rich result types opting into visible inline presentation.
- `safe.ts`: reads typed fields safely from tool result payloads.
