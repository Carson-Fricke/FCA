<script lang="ts">
	import type { ArtifactInput } from '$lib/types';

	/** Declared ingestion: one pill per subscription with exact symbols and interval. */
	let { inputs }: { inputs: ArtifactInput[] } = $props();
</script>

<div class="flex flex-wrap gap-1.5" data-testid="ingest-chips">
	{#each inputs as input}
		<span
			class="inline-flex items-center gap-1 rounded-full border border-accent/30 bg-accent/5 px-2 py-0.5 font-mono text-[10px] text-accent"
		>
			<span aria-hidden="true">◈</span>
			{input.data_type} {input.symbols.join(' / ')}{input.interval ? ` · ${input.interval}` : ''}
		</span>
	{:else}
		<span class="text-xs text-slate-500">No declared subscriptions.</span>
	{/each}
</div>
