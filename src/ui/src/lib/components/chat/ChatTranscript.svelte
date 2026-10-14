<script lang="ts">
	import { derivePhaseAttempts } from '$lib/subagent';
	import ConsentCard from './ConsentCard.svelte';
	import ScopeTranscript from './ScopeTranscript.svelte';
	import { setWorkflowLiveView } from './workflow-live-view';
	import { deriveWorkflowLive, isTerminalWorkflow, type WorkflowLiveState } from '$lib/workflow-live';
	import { buildNodeEntries, isToolRailEntry } from '$lib/transcript';
	import type { ChatMessage, ConsentConfirmation, ConsentProposal, ExecutionEvent, SteeringPercolationTarget } from '$lib/types';

	let { messages, terminal = false, events = [], workspaceId, percolations = {}, proposal = null,
		onConfirmConsent, onRejectConsent, consentBusy = false, workflows = null,
		onEnterPhase = () => {} }: {
		messages: ChatMessage[]; terminal?: boolean; events?: ExecutionEvent[]; workspaceId: string;
		percolations?: Record<string, SteeringPercolationTarget[]>; proposal?: ConsentProposal | null;
		onConfirmConsent?: (confirmation: ConsentConfirmation) => void; onRejectConsent?: () => void;
		consentBusy?: boolean; workflows?: Map<string, WorkflowLiveState> | null;
		onEnterPhase?: (executionId: string, phase: string, options: { readOnly: boolean }) => void;
	} = $props();

	// Execution metadata updates workflow cards; only meaningful activity renders in the root.
	const entries = $derived(buildNodeEntries({ messages: messages.filter((message) => message.kind !== 'sub_agent'), events, terminal }).filter((entry) => entry.kind !== 'event'));
	const liveWorkflows = $derived.by(() => {
		if (workflows) return workflows;
		const byExecution = new Map<string, ExecutionEvent[]>();
		for (const event of events) {
			const bucket = byExecution.get(event.executionId) ?? [];
			bucket.push(event);
			byExecution.set(event.executionId, bucket);
		}
		return deriveWorkflowLive(messages, byExecution);
	});
	setWorkflowLiveView((id) => {
		const live = liveWorkflows.get(id);
		return live ? { ...live, jobIds: derivePhaseAttempts(events.filter(event => event.executionId === id), live.execution.phase ?? "").slice(-1)[0]?.jobs ?? [], onEnterPhase: (phase, options) => onEnterPhase(id, phase,
			{ readOnly: options.readOnly || isTerminalWorkflow(live.execution) }) } : null;
	});
</script>

<div class="space-y-3" data-testid="chat-transcript">
	{#each entries as entry, index (entry.id)}
        <ScopeTranscript entries={[entry]} {workspaceId} {percolations} connectRail={isToolRailEntry(entry)&&isToolRailEntry(entries[index-1])} />
	{/each}
	{#if proposal && onConfirmConsent}
		<ConsentCard {proposal} onConfirm={onConfirmConsent} onReject={onRejectConsent} busy={consentBusy} />
	{/if}
</div>
