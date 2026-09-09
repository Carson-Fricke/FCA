<script lang="ts">
	import type { Snippet } from 'svelte';
	import ConsentCard from './ConsentCard.svelte';
	import EvidenceBadge from './EvidenceBadge.svelte';
	import type {
		ChatMessage,
		ConsentConfirmation,
		ConsentProposal,
		EvidenceItem,
		SteeringPercolationTarget
	} from '$lib/types';

	/**
	 * Transcript pane: role-styled messages, inline steering
	 * acknowledgements, uploaded-evidence trust tiers, evidence badges, and
	 * the structured activation-consent card when a proposal is open.
	 */
	let {
		messages,
		evidenceFor = () => [],
		percolations = {},
		proposal = null,
		onConfirmConsent,
		onRejectConsent,
		consentBusy = false
	}: {
		messages: ChatMessage[];
		evidenceFor?: (messageId: string) => EvidenceItem[];
		percolations?: Record<string, SteeringPercolationTarget[]>;
		proposal?: ConsentProposal | null;
		onConfirmConsent?: (confirmation: ConsentConfirmation) => void;
		onRejectConsent?: () => void;
		consentBusy?: boolean;
	} = $props();

	const ROLE_STYLE: Record<string, string> = {
		user: 'border-accent/40 bg-accent/5',
		assistant: 'border-white/10 bg-surface-850/60',
		tool: 'border-surface-600/60 bg-surface-900/60',
		system: 'border-surface-600/60 bg-surface-900/60'
	};
</script>

<div class="space-y-3" data-testid="chat-transcript">
	{#each messages as message (message.message_id)}
		<article
			class="rounded-xl border p-3 {ROLE_STYLE[message.role] ?? 'border-white/10 bg-surface-850/60'}"
			data-testid="chat-message"
			data-role={message.role}
			data-message-id={message.message_id}
		>
			<header class="mb-1 flex items-center gap-2 text-[10px] uppercase tracking-wider">
				<span
					class="font-semibold {message.role === 'user'
						? 'text-accent'
						: message.role === 'assistant'
							? 'text-slate-300'
							: 'text-slate-500'}"
				>
					{message.role}
				</span>
				<span class="font-mono text-slate-600">{message.actor_id}</span>
				{#if message.intent_epoch > 0}
					<span class="font-mono text-warn/70">epoch {message.intent_epoch}</span>
				{/if}
				<time class="ml-auto font-mono text-slate-600">{message.created_at.slice(11, 19)}</time>
			</header>
			<p class="text-sm whitespace-pre-wrap text-slate-200">{message.body}</p>

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

			{#if evidenceFor(message.message_id).length}
				<div class="mt-2 flex flex-wrap gap-1.5">
					{#each evidenceFor(message.message_id) as item (item.id)}
						<EvidenceBadge evidence={item} />
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
		</article>
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
