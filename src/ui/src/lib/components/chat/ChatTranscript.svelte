<script lang="ts">
	import ConsentCard from './ConsentCard.svelte';
	import ScopeTranscript from './ScopeTranscript.svelte';
	import WorkflowCard from './WorkflowCard.svelte';
	import { setWorkflowLiveView } from './workflow-live-view';
	import { deriveWorkflowLive, isTerminalWorkflow, type WorkflowLiveState } from '$lib/workflow-live';
	import { buildNodeEntries } from '$lib/transcript';
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

	const entries = $derived(buildNodeEntries({ messages: messages.filter((message) => message.kind !== 'sub_agent'), events, terminal }));
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
		return live ? { ...live, onEnterPhase: (phase, options) => onEnterPhase(id, phase,
			{ readOnly: options.readOnly || isTerminalWorkflow(live.execution) }) } : null;
	});
</script>

<div class="space-y-3" data-testid="chat-transcript">
	{#each entries as entry (entry.id)}
		{#if entry.kind === 'message' && entry.message.kind === 'workflow'}
			{@const id = String(entry.message.data?.executionId ?? entry.message.data?.execution_id ?? entry.message.execution_id ?? '')}
			{@const live = liveWorkflows.get(id)}
			<WorkflowCard execution={live?.execution} phases={live?.phases ?? []} status={live?.execution.state ?? 'running'}
				summary={live?.execution.phase ? `Current phase ${live.execution.phase}` : String(entry.message.data?.title ?? 'Workflow launched')}
				attentionPhases={live?.attentionPhases ?? []} onEnterPhase={(phase, options) => onEnterPhase(id, phase,
					{ readOnly: options.readOnly || isTerminalWorkflow(live?.execution) })} />
		{:else}
			<ScopeTranscript entries={[entry]} {workspaceId} {percolations} />
		{/if}
	{/each}
	{#if proposal && onConfirmConsent}
		<ConsentCard {proposal} onConfirm={onConfirmConsent} onReject={onRejectConsent} busy={consentBusy} />
	{/if}
</div>
