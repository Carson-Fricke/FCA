<script lang="ts">
	/**
	 * Saved studies: workspace-artifact screener definitions with their
	 * filter summaries and definition-hash provenance. Studies load back
	 * into the builder or re-run as-is. (Deletion is not offered: the
	 * screening HTTP surface has no DELETE route.)
	 */
	import { describeSpec } from './filterCatalog';
	import { shortHash } from '$lib/format';
	import type { ScreenerSavedDefinition } from '$lib/types';

	let {
		saved,
		onLoad,
		onRerun
	}: {
		saved: ScreenerSavedDefinition[];
		onLoad: (study: ScreenerSavedDefinition) => void;
		onRerun: (study: ScreenerSavedDefinition) => void;
	} = $props();
</script>

<section class="rounded-xl border border-surface-700 bg-surface-900 p-4" data-testid="screener-saved-panel">
	<h3 class="text-sm font-semibold text-slate-800">
		Saved screeners &amp; studies
		<span class="font-mono text-[10px] text-slate-500">({saved.length})</span>
	</h3>
	<ul class="mt-2 space-y-2" data-testid="saved-screeners">
		{#each saved as study (study.screener_id)}
			<li
				class="rounded-lg border border-surface-700 bg-surface-950 px-3 py-2"
				data-testid="saved-study"
				data-screener={study.screener_id}
			>
				<div class="flex items-baseline gap-2">
					<span class="truncate text-xs font-medium text-slate-800">{study.definition.name}</span>
					<span class="ml-auto font-mono text-[10px] text-slate-500" title={study.definition_hash}>
						{shortHash(study.definition_hash)}
					</span>
				</div>
				<p class="mt-0.5 truncate font-mono text-[10px] text-slate-500">{study.artifact_path}</p>
				<ul class="mt-1.5 flex flex-wrap gap-1">
					{#each study.definition.filters as filter, index (`${index}:${filter.kind}`)}
						<li class="rounded-full border border-surface-600 px-2 py-0.5 font-mono text-[10px] text-slate-500">
							{describeSpec(filter)}
						</li>
					{/each}
				</ul>
				<div class="mt-2 flex gap-2">
					<button
						type="button"
						class="rounded border border-accent/40 bg-accent/15 px-2 py-0.5 text-[10px] font-semibold text-accent hover:bg-accent/25"
						onclick={() => onLoad(study)}
						data-testid="load-study"
					>
						Load into builder
					</button>
					<button
						type="button"
						class="rounded border border-surface-600 px-2 py-0.5 text-[10px] text-slate-500 hover:text-slate-800"
						onclick={() => onRerun(study)}
						data-testid="rerun-study"
					>
						Re-run
					</button>
				</div>
			</li>
		{:else}
			<li class="text-[11px] text-slate-500">
				Nothing saved yet — build filters and save them as workspace artifacts.
			</li>
		{/each}
	</ul>
</section>
