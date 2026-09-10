<script lang="ts">
	/**
	 * Run results: full provenance strip (result id, as-of, universe
	 * revision, data window, definition hash) plus a rank/symbol table whose
	 * metric columns come from the first hit's fact-sheet keys. Loading,
	 * empty and error states follow the shared visual language.
	 */
	import EmptyState from '$lib/components/common/EmptyState.svelte';
	import { shortHash } from '$lib/format';
	import type { ScreenerRunResult } from '$lib/types';

	let {
		result = null,
		running = false,
		error = null
	}: {
		result?: ScreenerRunResult | null;
		running?: boolean;
		error?: string | null;
	} = $props();

	const metricKeys = $derived(result?.hits[0] ? Object.keys(result.hits[0].metrics) : []);
	const header = (key: string): string => key.replaceAll('_', ' ');
</script>

<section class="rounded-xl border border-surface-700 bg-surface-900" data-testid="screener-results">
	<div class="flex items-center gap-2 border-b border-surface-700 px-4 py-2.5">
		<h3 class="text-sm font-semibold text-slate-800">Results</h3>
		{#if running}
			<span
				class="rounded-full border border-accent/40 px-2 py-0.5 text-[10px] text-accent"
				data-testid="screener-running"
			>
				screening…
			</span>
		{/if}
	</div>

	{#if error}
		<p class="border-b border-loss/40 bg-loss/10 px-4 py-2 font-mono text-[10px] text-loss" data-testid="results-error">
			{error}
		</p>
	{/if}

	{#if result}
		<div
			class="flex flex-wrap gap-x-4 gap-y-1 border-b border-surface-700 px-4 py-2 font-mono text-[10px] text-slate-500"
			data-testid="screener-provenance"
		>
			<span>result {result.result_id}</span>
			<span>definition {result.definition_name}</span>
			<span>as of {result.as_of}</span>
			<span>universe {result.universe_revision}</span>
			<span>window {result.data_window_start} → {result.data_window_end}</span>
			<span title={result.definition_hash}>hash {shortHash(result.definition_hash)}</span>
		</div>
		{#if result.hits.length}
			<div class="overflow-x-auto">
				<table class="w-full border-collapse font-mono text-xs">
					<thead>
						<tr class="text-left text-[10px] tracking-wider text-slate-500 uppercase">
							<th class="px-4 py-2">Rank</th>
							<th class="px-4 py-2">Symbol</th>
							{#each metricKeys as key (key)}
								<th class="px-4 py-2 text-right">{header(key)}</th>
							{/each}
						</tr>
					</thead>
					<tbody>
						{#each result.hits as hit (`${hit.rank}:${hit.symbol}`)}
							<tr class="border-t border-surface-700" data-testid="screener-row" data-symbol={hit.symbol}>
								<td class="px-4 py-1.5 text-slate-500">{hit.rank}</td>
								<td class="px-4 py-1.5 font-semibold text-slate-800">{hit.symbol}</td>
								{#each metricKeys as key (key)}
									<td class="px-4 py-1.5 text-right text-slate-700">{hit.metrics[key] ?? '—'}</td>
								{/each}
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{:else}
			<div class="p-4">
				<EmptyState
					title="No survivors"
					hint="Every symbol in the as-of universe screened out."
				/>
			</div>
		{/if}
	{:else}
		<div class="p-4">
			<EmptyState
				title="No screening results yet"
				hint={running ? 'Screening the as-of universe…' : 'Configure filters and run the screener to populate results.'}
			/>
		</div>
	{/if}
</section>
