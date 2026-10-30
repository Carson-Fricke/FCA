# Conversation presentation

These components render canonical conversation timelines and accepted workflow
artifacts. They read job status for displayed execution cards and artifact manifests on operator request;
they do not dispatch research executions or calculate financial statistics.
`tool-results/` owns the specialized tool-outcome cards. Shared decoding belongs
to `lib/transcript.ts`, and backend contracts remain authoritative.

- `ChatComposer.svelte`: conversation input and attachments; clears immediately and restores failed drafts only if no newer text exists.
- `ChatStarters.svelte`: suggested initial conversation inputs.
- `ChatSurface.svelte`: shared chat composition, fullscreen outline navigation and anchored receipt reveals, automatic earlier-history loading with retry, scoped workflow cards, and active scope input; an unresolved phase agent is unavailable, never inferred to be deterministic.
- `ChatTranscript.svelte`: root message and structured activity presentation; execution metadata updates workflow cards without rendering raw event rows.
- `ConsentCard.svelte`: activation consent presentation.
- `ConversationDeleteDialog.svelte`: conversation deletion interaction.
- `ConversationOutline.svelte`: compact request-grouped navigation to workflows, conversations, studies, and artifacts; native row disclosures and separate name navigation, a persisted pointer/keyboard-resizable width, and retained loading failures with retry.
- `EvidenceBadge.svelte`: evidence status marker.
- `FalsificationReportCard.svelte`: accepted falsification trials and controls.
- `HarnessExecutionCard.svelte`: shared observational job status, dataset counters,
  durations and failure details for direct runs and workflow views.
- `KnowledgeSearch.svelte`: knowledge lookup interaction.
- `MarkdownBody.svelte`: safe Markdown body rendering.
- `MessageDetailButton.svelte`: explicit canonical evidence expansion for a bounded message preview.
- `transcript-details.ts`: scoped detail-loading port through the current timeline owner.
- `PhaseRunView.svelte`: phase attempts, activity, recovery diagnostics, and accepted output; missing activity is shown as starting or unavailable.
- `ScopeStack.svelte`: wrapping, fully readable conversation breadcrumbs, arrow-only parent navigation, scope kind, and host controls above the shared chat/outline body.
- `ScopeTranscript.svelte`: one scope's unified timeline, with rail continuity
  between consecutive tool entries, pending/failed send status, and an optional preceding root-tool context.
- `SubAgentComposer.svelte`: named agent-scope input with immediate clearing and conditional failed-draft restoration.
- `ThinkingAccordion.svelte`: Collapsed reasoning disclosure shared by provisional and completed output; expansion survives completion.
- `TranscriptViewport.svelte`: shared root/child/phase viewport, automatic earlier-history loading with prepend anchoring, per-scope reading positions, resize-aware bottom following, and an arrow-only jump control hidden at the bottom.
- `ToolResultCard.svelte`: Activity rail presentation and specialized result
  dispatch; compact routine receipts, visible rich results and operator requests,
  and retained execution metadata. Root and scoped transcripts share this shell.
- `VariationReportCard.svelte`: development and forward evidence tables,
  marginal and declared joint plots, matched comparisons, complete evidence
  export, and on-demand job and artifact inspection. Matched vectors use the
  backend canonical parameter identity rather than reconstructed UI hashes.
- `WorkflowCard.svelte`: workflow summary, independently active graph nodes, read-only pending input ports, and noninteractive fan-out dots synchronized with graph pan and zoom.
- `sub-agent-live-view.ts`: live subagent view projections.
- `workflow-live-view.ts`: live workflow view projections.
