<script lang="ts">
	/**
	 * Prompt recommendations for an empty conversation. Selecting one sends it
	 * as the first message (creating the thread if needed), mirroring the
	 * prototype's chat starters.
	 */
	let {
		onSelect,
		disabled = false
	}: { onSelect: (prompt: string) => void; disabled?: boolean } = $props();

	const STARTERS = [
		{
			prompt: 'Analyze my performance',
			title: 'Analyze my performance',
			detail: 'Find the signal in your results'
		},
		{
			prompt: 'Help me build a new strategy',
			title: 'Build a new strategy',
			detail: 'Turn a trading idea into code'
		},
		{
			prompt: 'Compare my backtest results',
			title: 'Compare my backtests',
			detail: 'See what’s working, and why'
		}
	];
</script>

<div class="grid gap-2" data-testid="chat-starters">
	{#each STARTERS as starter (starter.prompt)}
		<button
			type="button"
			class="flex items-center gap-3 rounded-lg border border-surface-700 bg-surface-900 p-3 text-left transition hover:border-accent/40 hover:bg-surface-800 disabled:cursor-not-allowed disabled:opacity-50"
			{disabled}
			onclick={() => onSelect(starter.prompt)}
			data-testid="chat-starter"
			data-prompt={starter.prompt}
		>
			<span class="min-w-0 flex-1">
				<span class="block text-sm font-medium text-slate-800">{starter.title}</span>
				<span class="block text-xs text-slate-500">{starter.detail}</span>
			</span>
			<span class="text-slate-500" aria-hidden="true">↗</span>
		</button>
	{/each}
</div>
