<script lang="ts">
	/**
	 * Message composer. Steering is a normal message: when an execution is
	 * active, sending percolates to it and the acknowledgement renders
	 * inline in the transcript. Knowledge search is an agent tool the
	 * workspace agent invokes itself; it never surfaces in the composer.
	 */
	let {
		onSubmit,
		activeExecutionId = null,
		busy = false
	}: {
		onSubmit: (input: { body: string; attachmentIds: string[]; steeringIntent: string | null }) => void;
		activeExecutionId?: string | null;
		busy?: boolean;
	} = $props();

	let body = $state('');

	const canSend = $derived(body.trim().length > 0 && !busy);

	function send(): void {
		if (!canSend) return;
		const steering = activeExecutionId && isSteering(body) ? body.trim() : null;
		onSubmit({
			body: body.trim(),
			attachmentIds: [],
			steeringIntent: steering
		});
		body = '';
	}

	function isSteering(text: string): boolean {
		// A message steers when an execution is active; the API decides the
		// percolation. The composer only marks intent for the ack display.
		return /\b(steer|focus|restrict|skip|instead|priorit)/i.test(text);
	}
</script>

<div class="border-t border-surface-700 bg-surface-900 p-3" data-testid="chat-composer">
	<div class="flex items-end gap-2">
		<textarea
			class="max-h-32 min-h-10 flex-1 resize-y rounded-lg border border-surface-600/60 bg-surface-950 px-3 py-2 text-sm text-slate-800 outline-none placeholder:text-slate-500 focus:border-accent/50"
			placeholder={activeExecutionId
				? 'Message the workspace agent — an execution is active; messages percolate as steering…'
				: 'Message the workspace agent…'}
			bind:value={body}
			onkeydown={(e) => {
				if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) send();
			}}
		></textarea>
		<button
			type="button"
			class="rounded-lg border border-accent/40 bg-accent/15 px-3 py-2 text-sm font-semibold text-accent transition hover:bg-accent/25 disabled:cursor-not-allowed disabled:opacity-40"
			disabled={!canSend}
			onclick={send}
			data-testid="composer-send"
		>
			Send
		</button>
	</div>

	{#if activeExecutionId}
		<div class="mt-2 flex items-center gap-1 font-mono text-[10px] text-warn/80">
			<span class="h-1.5 w-1.5 animate-pulse rounded-full bg-warn" aria-hidden="true"></span>
			execution active · messages percolate (durable ack)
		</div>
	{/if}
</div>
