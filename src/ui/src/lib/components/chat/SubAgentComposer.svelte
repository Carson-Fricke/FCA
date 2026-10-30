<script lang="ts">

	/**
	 * Capability-gated composer for one sub-agent node. The composer is
	 * rendered only when the node declares `accepts_input`; a node that does
	 * not accept input has no composer at all (never a disabled one).
	 */
	let {
		acceptsInput,
		phase = '',
		agentLabel = '',
		busy = false,
		draft = $bindable(''),
		onSubmit = () => {}
	}: {
		acceptsInput: boolean;
		phase?: string;
		agentLabel?: string;
		busy?: boolean;
		draft?: string;
		onSubmit?: (body: string) => void | Promise<void>;
	} = $props();

	let submitting = $state(false);
	const canSend = $derived(acceptsInput && draft.trim().length > 0 && !busy && !submitting);
	const phaseName = $derived(phase);
	const placeholder = $derived(phaseName ? `Message the ${phaseName.replace(/_/g, ' ')} agent...` : 'Message this sub-agent...');

	async function send(): Promise<void> {
		if (!canSend) return;
		const sentBody = draft;
		draft = '';
		submitting = true;
		try {
			await onSubmit(sentBody.trim());
		} catch {
			if (!draft) draft = sentBody;
			return;
		} finally {
			submitting = false;
		}
	}
</script>

{#if acceptsInput}
	<div class="border-t border-surface-700 bg-surface-900 p-2.5" data-testid="sub-agent-composer">
		{#if agentLabel}<p class="mb-1.5 text-[11px] text-slate-400">Message {agentLabel.replace(/_/g, ' ')}</p>{/if}
		<div class="flex items-end gap-2">
			<textarea
				class="max-h-28 min-h-9 flex-1 resize-y rounded-lg border border-surface-600/60 bg-surface-950 px-3 py-1.5 text-sm text-slate-800 outline-none placeholder:text-slate-500 focus:border-accent/50"
				{placeholder}
				aria-label={agentLabel ? `Message ${agentLabel}` : placeholder}
				bind:value={draft}
				data-testid="sub-agent-composer-input"
				onkeydown={(e) => {
					if (e.key !== 'Enter' || e.isComposing || e.shiftKey) return;
					e.preventDefault();
					send();
				}}
			></textarea>
			<button
				type="button"
				class="rounded-lg border border-accent/40 bg-accent/15 px-3 py-1.5 text-sm font-semibold text-accent transition hover:bg-accent/25 disabled:cursor-not-allowed disabled:opacity-40"
				disabled={!canSend}
				onclick={send}
				data-testid="sub-agent-composer-send"
			>
				Send
			</button>
		</div>
	</div>
{/if}
