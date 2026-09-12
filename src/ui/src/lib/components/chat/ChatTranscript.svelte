<script lang="ts">
	import ConsentCard from './ConsentCard.svelte';
	import MarkdownBody from './MarkdownBody.svelte';
	import ThinkingAccordion from './ThinkingAccordion.svelte';
	import ToolResultCard from './ToolResultCard.svelte';
	import type {
		ChatMessage,
		ConsentConfirmation,
		ConsentProposal,
		SteeringPercolationTarget
	} from '$lib/types';

	/**
	 * Transcript pane: role-aligned messages (user bubbles right-aligned on
	 * the soft green background; assistant messages left-aligned with no
	 * background), inline steering acknowledgements, and the structured
	 * activation-consent card when a proposal is open. Evidence scoping
	 * belongs to sandboxed subagent views, not to user-visible chat
	 * messages.
	 */
	let {
		messages,
		workspaceId,
		percolations = {},
		proposal = null,
		onConfirmConsent,
		onRejectConsent,
		consentBusy = false
	} = $props();

	const isUser = (role: string) => role === 'user';
</script>

<div class="space-y-3" data-testid="chat-transcript">
	{#each messages as message (message.message_id)}
		{#if message.role === 'tool'}
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
