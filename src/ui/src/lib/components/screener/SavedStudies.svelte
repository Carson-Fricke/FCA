<script lang="ts">
	/**
	 * Saved screener definitions: content-addressed workspace artifacts with
	 * their definition hash, run configuration, and authored-source preview.
	 * A definition loads back into the authoring surface or re-runs as-is.
	 * (Deletion is not offered: the screener HTTP surface has no DELETE route.)
	 */
	import { shortHash } from '$lib/format';
	import type { ScreenerDefinition } from '$lib/types';

	let {
		saved,
		selectedId = null,
		onLoad,
		onRun
	}: {
		saved: ScreenerDefinition[];
		selectedId?: string | null;
		onLoad: (definition: ScreenerDefinition) => void;
		onRun: (definition: ScreenerDefinition) => void;
	} = $props();

	function sourcePreview(source: string): string {
		const line = source
			.split('\n')
			.map((candidate) => candidate.trim())
			.find((candidate) => candidate.startsWith('// filter:'));
		return line ? line.replace('// filter:', '').trim() : 'authored program';
	}
</script>

<section class="rounded-xl border border-surface-700 bg-surface-900 p-4" data-testid="screener-saved-panel">
	<h3 class="text-sm font-semibold text-slate-800">
		Saved screeners
		<span class="font-mono text-[10px] text-slate-500">({saved.length})</span>
	</h3>
	<ul class="mt-2 space-y-2" data-testid="saved-screeners">
		{#each saved as definition (definition.definitionId)}
			<li
				class="rounded-lg border bg-surface-950 px-3 py-2 {definition.definitionId === selectedId
					? 'border-accent/50'
					: 'border-surface-700'}"
				data-testid="saved-study"
				data-definition-id={definition.definitionId}
			>
				<div class="flex items-baseline gap-2">
					<span class="truncate text-xs font-medium text-slate-800">{definition.name}</span>
					<span
						class="ml-auto font-mono text-[10px] text-slate-500"
						title={definition.definitionSha256}
						data-testid="saved-study-hash"
					>
						{shortHash(definition.definitionSha256)}
					</span>
				</div>
				<p class="mt-0.5 font-mono text-[10px] text-slate-500">
					{definition.barInterval} · {definition.lookbackDays}d · rank {definition.rankMetric}
				</p>
				<p class="mt-0.5 truncate font-mono text-[10px] text-slate-500">
					{sourcePreview(definition.source)}
				</p>
				<div class="mt-2 flex gap-2">
					<button
						type="button"
						class="rounded border border-accent/40 bg-accent/15 px-2 py-0.5 text-[10px] font-semibold text-accent hover:bg-accent/25"
						onclick={() => onLoad(definition)}
						data-testid="load-study"
					>
						Load
					</button>
					<button
						type="button"
						class="rounded border border-surface-600 px-2 py-0.5 text-[10px] text-slate-500 hover:text-slate-800"
						onclick={() => onRun(definition)}
						data-testid="rerun-study"
					>
						Re-run
					</button>
				</div>
			</li>
		{:else}
			<li class="text-[11px] text-slate-500">
				Nothing saved yet — author filters or a Rust program and save it as a workspace artifact.
			</li>
		{/each}
	</ul>
</section>
