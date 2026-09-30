<script lang="ts">
	/**
	 * Capability-gated composer for one sub-agent node. The composer is
	 * rendered only when the node declares `accepts_input`; a node that does
	 * not accept input has no composer at all (never a disabled one).
	 */
	let {
		acceptsInput,
		phase = '',
		busy = false,
		onSubmit = () => {}
	}: {
		acceptsInput: boolean;
		phase?: string;
		busy?: boolean;
		onSubmit?: (body: string) => void | Promise<void>;
	} = $props();

	let body = $state('');
	const canSend = $derived(acceptsInput && body.trim().length > 0 && !busy);
	const placeholder = $derived(
		phase === 'verdict'
			? 'Say "check off" to apply the verdict, or "circle back" to revise the hypothesis...'
			: phase === 'hypothesis_creation'
				? 'Confirm to approve hypothesis (e.g. "approved"), or message feedback to revise...'
				: 'Message this sub-agent...'
	);

	async function send(): Promise<void> {
		if (!canSend) return;
		const draft = body.trim();
		try {
			await onSubmit(draft);
		} catch {
			// Keep the draft so a rejected send can be retried or edited.
			return;
		}
		body = '';
	}
</script>

{#if acceptsInput}
	<div class="border-t border-surface-700 bg-surface-900 p-2.5" data-testid="sub-agent-composer">
		<div class="flex items-end gap-2">
			<textarea
				class="max-h-28 min-h-9 flex-1 resize-y rounded-lg border border-surface-600/60 bg-surface-950 px-3 py-1.5 text-sm text-slate-800 outline-none placeholder:text-slate-500 focus:border-accent/50"
				{placeholder}
				bind:value={body}
				disabled={busy}
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
