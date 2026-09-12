<script lang="ts">
	import ConsentCard from './ConsentCard.svelte';
	import MarkdownBody from './MarkdownBody.svelte';
	import ThinkingAccordion from './ThinkingAccordion.svelte';
	import ToolResultCard from './ToolResultCard.svelte';
	import WorkflowCard from './WorkflowCard.svelte';
	import type {
		ChatMessage,
		ConsentConfirmation,
		ConsentProposal,
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
	 * anchor upgrades to the current phase state and full enter/link
	 * affordances; otherwise the card is derived from the anchor's own `data`.
	 */
	let {
		messages,
		workspaceId,
		percolations = {},
		proposal = null,
		onConfirmConsent,
		onRejectConsent,
		consentBusy = false,
		execution = null,
		phases = [],
		onEnterPhase = () => {},
		fullPageHref = null
	}: {
		messages: ChatMessage[];
		workspaceId: string;
		percolations?: Record<string, SteeringPercolationTarget[]>;
		proposal?: ConsentProposal | null;
		onConfirmConsent?: (confirmation: ConsentConfirmation) => void;
		onRejectConsent?: () => void;
		consentBusy?: boolean;
		execution?: WorkflowExecutionRecord | null;
		phases?: WorkflowPhase[];
		onEnterPhase?: (phase: string, options: { readOnly: boolean }) => void;
		fullPageHref?: string | null;
	} = $props();

	const isUser = (role: string) => role === 'user';

	/** The live execution when it matches the anchor, else a minimal record. */
	function anchorExecution(message: ChatMessage): WorkflowExecutionRecord | null {
		const data = message.data ?? {};
		const executionId = String(data.executionId ?? message.execution_id ?? '');
		if (execution && execution.execution_id === executionId) return execution;
		if (!executionId) return null;
		return {
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
	}

	function anchorPhases(message: ChatMessage): WorkflowPhase[] {
		const anchor = anchorExecution(message);
		if (execution && anchor && execution.execution_id === anchor.execution_id) {
			return phases;
		}
		return anchor?.phase ? [{ name: anchor.phase, status: 'active' }] : [];
	}

	function anchorStatus(message: ChatMessage): string {
		const data = message.data ?? {};
		return String(data.status ?? anchorExecution(message)?.state ?? 'running');
	}

	function anchorSummary(message: ChatMessage): string {
		const data = message.data ?? {};
		const phase = data.currentPhase == null ? null : String(data.currentPhase);
		return phase ? `Current phase ${phase}` : (String(data.title ?? '') || 'Workflow launched');
	}

	function anchorHref(message: ChatMessage): string | null {
		if (fullPageHref) return fullPageHref;
		const executionId = anchorExecution(message)?.execution_id;
		return executionId ? `/w/${workspaceId}/workflows/${executionId}` : null;
	}
</script>

<div class="space-y-3" data-testid="chat-transcript">
	{#each messages as message (message.message_id)}
		{#if message.kind === 'workflow'}
			<WorkflowCard
				execution={anchorExecution(message)}
				phases={anchorPhases(message)}
				status={anchorStatus(message)}
				summary={anchorSummary(message)}
				fullPageHref={anchorHref(message)}
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
