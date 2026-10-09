# Conversation presentation

These components render canonical conversation timelines and accepted workflow
artifacts. They may read a job and its artifact manifest on operator request;
they do not dispatch research executions or calculate financial statistics.
`tool-results/` owns the specialized tool-outcome cards. Shared decoding belongs
to `lib/transcript.ts`, and backend contracts remain authoritative.

- `ChatComposer.svelte`: standard conversation input and attachments.
- `ChatStarters.svelte`: suggested initial conversation inputs.
- `ChatSurface.svelte`: conversation page composition and active scope input.
- `ChatTranscript.svelte`: root conversation timeline presentation.
- `ConsentCard.svelte`: activation consent presentation.
- `ConversationDeleteDialog.svelte`: conversation deletion interaction.
- `EvidenceBadge.svelte`: evidence status marker.
- `FalsificationReportCard.svelte`: accepted falsification trials and controls.
- `KnowledgeSearch.svelte`: knowledge lookup interaction.
- `MarkdownBody.svelte`: safe Markdown body rendering.
- `PhaseRunView.svelte`: phase attempts, activity, and accepted output.
- `ScopeStack.svelte`: nested conversation scope presentation.
- `ScopeTranscript.svelte`: one scope's unified timeline.
- `StreamingReply.svelte`: incremental reply presentation.
- `SubAgentComposer.svelte`: standard input addressed to an agent scope.
- `ThinkingAccordion.svelte`: expandable reasoning presentation.
- `ToolResultCard.svelte`: tool outcome dispatch to the specialized cards.
- `VariationReportCard.svelte`: development and forward evidence tables,
  marginal and declared joint plots, matched comparisons, complete evidence
  export, and on-demand job and artifact inspection. Matched vectors use the
  backend canonical parameter identity rather than reconstructed UI hashes.
- `WorkflowCard.svelte`: workflow summary and phases.
- `sub-agent-live-view.ts`: live subagent view projections.
- `workflow-live-view.ts`: live workflow view projections.
