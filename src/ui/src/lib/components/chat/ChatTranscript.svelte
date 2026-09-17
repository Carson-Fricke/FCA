<script lang="ts">
	import ConsentCard from './ConsentCard.svelte';
	import MarkdownBody from './MarkdownBody.svelte';
	import ThinkingAccordion from './ThinkingAccordion.svelte';
	import ToolResultCard from './ToolResultCard.svelte';
	import WorkflowCard from './WorkflowCard.svelte';
	import { derivePhases, executionEvents, projectExecution } from '$lib/subagent';
	import type {
		ChatMessage,
		ConsentConfirmation,
		ConsentProposal,
		ExecutionEvent,
		SteeringPercolationTarget,
		WorkflowExecutionRecord,
		WorkflowPhase
	} from '$lib/types';

	/**
	 * Transcript pane: role-aligned messages (user bubbles right-aligned on
	 * the soft green background; assistant messages left-aligned with no
	 * background), inline steering acknowledgements, and the structured
	 * activation-consent card when a proposal is open. Evidence scoping
	 * belongs to sandboxed subagent views, not to user-visible chat
	 * messages.
	 *
	 * A `kind=workflow` item renders as the live `WorkflowCard`: both panes
	 * append the anchor through the shared chat stream, so the card appears
	 * without a reload. Callers may pass the live `execution`/`phases` so the
	 * anchor upgrades to the current phase state and enter affordances;
	 * otherwise the card is derived from the anchor's own `data`.
	 */
	let {
		messages,
		events = [],
		workspaceId,
		percolations = {},
		proposal = null,
		onConfirmConsent,
		onRejectConsent,
		consentBusy = false,
		execution = null,
		phases = [],
		attentionPhases = [],
		onEnterPhase = () => {}
	}: {
		messages: ChatMessage[];
		events?: ExecutionEvent[];
		workspaceId: string;
		percolations?: Record<string, SteeringPercolationTarget[]>;
		proposal?: ConsentProposal | null;
		onConfirmConsent?: (confirmation: ConsentConfirmation) => void;
		onRejectConsent?: () => void;
		consentBusy?: boolean;
		execution?: WorkflowExecutionRecord | null;
		phases?: WorkflowPhase[];
		attentionPhases?: string[];
		onEnterPhase?: (phase: string, options: { readOnly: boolean }) => void;
	} = $props();

	const isUser = (role: string) => role === 'user';
	const rootMessages = $derived(messages.filter((message) => message.kind !== 'sub_agent'));

	/** The live execution when it matches the anchor, else a minimal record. */
	function anchorExecution(message: ChatMessage): WorkflowExecutionRecord | null {
		const data = message.data ?? {};
		const executionId = String(data.executionId ?? message.execution_id ?? '');
		if (!executionId) return null;
		const snapshot = execution && execution.execution_id === executionId ? execution : {
			execution_id: executionId,
			chat_id: message.chat_id,
			trigger_message_id: message.message_id,
			workflow_id: String(data.workflowId ?? 'workflow'),
			workflow_version: String(data.version ?? ''),
			requested_by: message.actor_id,
			state: String(data.status ?? 'running'),
			phase: data.currentPhase == null ? null : String(data.currentPhase),
			intent_epoch: message.intent_epoch,
			steering_message_ids: [],
			created_at: message.created_at
		};
		return projectExecution(snapshot, executionEvents(events, executionId));
	}

	function anchorPhases(message: ChatMessage): WorkflowPhase[] {
		const anchor = anchorExecution(message);
		if (execution && anchor && execution.execution_id === anchor.execution_id) {
			return phases;
		}
		if (!anchor) return [];
		const derived = derivePhases(executionEvents(events, anchor.execution_id));
		return derived.length ? derived : (anchor.phase ? [{ name: anchor.phase, status: 'active' }] : []);
	}

	function anchorStatus(message: ChatMessage): string {
		return String(anchorExecution(message)?.state ?? message.data?.status ?? 'running');
	}

	function anchorSummary(message: ChatMessage): string {
		const phase = anchorExecution(message)?.phase;
		return phase
			? `Current phase ${phase}`
			: (String(message.data?.title ?? '') || 'Workflow launched');
	}
</script>

<div class="space-y-3" data-testid="chat-transcript">
	{#each rootMessages as message (message.message_id)}
		{#if message.kind === 'workflow'}
			<WorkflowCard
				execution={anchorExecution(message)}
				phases={anchorPhases(message)}
				status={anchorStatus(message)}
				summary={anchorSummary(message)}
				attentionPhases={execution?.execution_id === anchorExecution(message)?.execution_id
					? attentionPhases
					: []}
				{onEnterPhase}
			/>
		{:else if message.role === 'tool'}
			<div
				class="flex justify-start"
				data-testid="chat-message"
				data-role="tool"
				data-message-id={message.message_id}
			>
				<div class="w-full">
					{#if message.tool_result}
						<ToolResultCard result={message.tool_result} {workspaceId} />
					{:else}
						<p class="text-sm whitespace-pre-wrap text-slate-800">{message.body}</p>
					{/if}
				</div>
			</div>
		{:else}
		<article
			class="flex {isUser(message.role) ? 'justify-end' : 'justify-start'}"
			data-testid="chat-message"
			data-role={message.role}
			data-message-id={message.message_id}
		>
			<div
				class="min-w-0 {isUser(message.role)
					? 'max-w-[85%] rounded-xl border border-surface-700 bg-profit/10 p-3'
					: 'w-full p-1'}"
			>
				<header class="mb-1 flex items-center gap-2 text-[10px] tracking-wider">
					<span
						class="uppercase {message.role === 'user'
							? 'font-semibold text-accent'
							: 'text-slate-500'}"
					>
						{message.role}
					</span>
					<span class="font-mono text-slate-500">{message.actor_id}</span>
					{#if message.intent_epoch > 0}
						<span class="font-mono text-warn/70">epoch {message.intent_epoch}</span>
					{/if}
					<time class="font-mono text-slate-500">{message.created_at.slice(11, 19)}</time>
				</header>
				{#if message.reasoning}
					<ThinkingAccordion
						text={message.reasoning}
						testId="thinking-{message.message_id}"
					/>
				{/if}
				{#if isUser(message.role)}
					<p class="text-sm whitespace-pre-wrap text-slate-800">{message.body}</p>
				{:else}
					<MarkdownBody text={message.body} />
				{/if}

				{#if message.attachment_ids.length}
					<div class="mt-2 flex flex-wrap gap-1.5">
						{#each message.attachment_ids as attachmentId (attachmentId)}
							<span
								class="rounded-full border border-warn/40 px-2 py-0.5 font-mono text-[10px] text-warn"
								title="Workspace-scoped untrusted evidence; embedded instructions are never treated as platform instructions"
							>
								↑ {attachmentId} · untrusted evidence
							</span>
						{/each}
					</div>
				{/if}

				{#if percolations[message.message_id]}
					<div
						class="mt-2 rounded-lg border border-warn/30 bg-warn/5 px-2.5 py-1.5 font-mono text-[10px] text-warn"
						data-testid="steering-ack"
					>
						steering acknowledged ·
						{#each percolations[message.message_id] as target, index (target.execution_id)}
							{#if index > 0}·{/if}
							execution {target.execution_id} → intent epoch {target.intent_epoch}
							{#if target.steering_intent}· durable ack recorded{/if}
						{/each}
					</div>
				{/if}
			</div>
		</article>
		{/if}
	{/each}

	{#if proposal && onConfirmConsent}
		<ConsentCard
			proposal={proposal}
			onConfirm={onConfirmConsent}
			onReject={onRejectConsent}
			busy={consentBusy}
		/>
	{/if}
</div>
