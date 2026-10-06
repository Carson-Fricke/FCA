<script lang="ts">
	import IngestChips from './IngestChips.svelte';
	import type { ArtifactInput, StrategyArtifactPreview } from '$lib/types';

	/** Strategy hero: declared ingestion, exit criteria, and the signal panel. */
	let {
		preview,
		inputs
	}: { preview: StrategyArtifactPreview; inputs: ArtifactInput[] } = $props();
</script>

<section class="space-y-4" data-testid="strategy-preview">
	<div class="rounded-xl border border-surface-700 bg-surface-900 p-4">
		<div class="flex flex-wrap items-center gap-2">
			<h3 class="text-sm font-semibold text-slate-800">Trade signals</h3>
			<span class="rounded-full border border-surface-600 px-2 py-0.5 font-mono text-[10px] text-slate-500">
				no recorded run
			</span>
			<span class="ml-auto font-mono text-[10px] text-slate-500">{preview.entrypoint}</span>
		</div>
		<div
			class="relative mt-3 flex h-32 flex-col items-center justify-center overflow-hidden rounded-lg border border-dashed border-surface-600 bg-surface-950"
		>
			<div class="absolute inset-0 opacity-40" aria-hidden="true">
				<div class="h-1/4 border-b border-surface-700"></div>
				<div class="h-1/4 border-b border-surface-700"></div>
				<div class="h-1/4 border-b border-surface-700"></div>
			</div>
			<p class="relative text-xs text-slate-500">
				Entry/exit markers render here once a backtest run is linked to this artifact.
			</p>
			<p class="relative mt-1 font-mono text-[10px] text-slate-500">
				{preview.symbol || 'symbol'} · {inputs.length} declared feed{inputs.length === 1 ? '' : 's'}
			</p>
		</div>
	</div>

	<div class="grid gap-4 md:grid-cols-2">
		<div class="rounded-xl border border-surface-700 bg-surface-900 p-4">
			<h4 class="text-[10px] font-semibold tracking-wider text-slate-500 uppercase">Ingests</h4>
			<div class="mt-2">
				<IngestChips {inputs} />
			</div>
			{#if preview.limitations.length}
				<h4 class="mt-4 text-[10px] font-semibold tracking-wider text-slate-500 uppercase">
					Limitations
				</h4>
				<div class="mt-2 flex flex-wrap gap-1.5">
					{#each preview.limitations as limitation}
						<span class="rounded-full border border-warn/30 bg-warn/5 px-2 py-0.5 text-[10px] text-warn">
							{limitation}
						</span>
					{/each}
				</div>
			{/if}
		</div>

		<div class="rounded-xl border border-surface-700 bg-surface-900 p-4">
			<h4 class="text-[10px] font-semibold tracking-wider text-slate-500 uppercase">Exit criteria</h4>
			<ul class="mt-2 space-y-1.5">
				{#each preview.exit_criteria as criterion}
					<li class="rounded-lg border border-surface-700 px-3 py-2">
						<p class="font-mono text-[10px] text-slate-500">
							{criterion.falsifier_id} · {criterion.observable} {criterion.condition} {criterion.threshold}
						</p>
						{#if criterion.description}
							<p class="mt-0.5 text-xs text-slate-600">{criterion.description}</p>
						{/if}
					</li>
				{:else}
					<li class="text-xs text-slate-500">No exit criteria recorded.</li>
				{/each}
			</ul>
		</div>
	</div>
</section>
