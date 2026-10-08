<script lang="ts">
	/**
	 * Screening run results: the full provenance strip (result id, definition
	 * id/hash, job id, as-of, artifact status, data window when published)
	 * plus a rank/symbol table whose metric columns come from the first row's
	 * recorded metrics. Loading, empty and error states follow the shared
	 * visual language.
	 */
	import EmptyState from '$lib/components/common/EmptyState.svelte';
	import { shortHash } from '$lib/format';
	import type { ScreeningResult } from '$lib/types';

	let {
		result = null,
		running = false,
		error = null,
		definitionName = null
	}: {
		result?: ScreeningResult | null;
		running?: boolean;
		error?: string | null;
		definitionName?: string | null;
	} = $props();

	function text(value: unknown): string | null {
		return typeof value === 'string' && value.trim() ? value : null;
	}

	const screenDocument = $derived(
		result?.provenance && typeof result.provenance.screen === 'object' && result.provenance.screen !== null
			? (result.provenance.screen as Record<string, unknown>)
			: {}
	);

	const dataWindow = $derived.by(() => {
		const window = screenDocument.data_window;
		if (typeof window !== 'object' || window === null) return null;
		const start = text((window as Record<string, unknown>).start);
		const end = text((window as Record<string, unknown>).end);
		return start && end ? `${start} → ${end}` : null;
	});

	const rankMetric = $derived(text(screenDocument.rank_metric));
	const metricKeys = $derived(result?.rows[0] ? Object.keys(result.rows[0].metrics) : []);
	const header = (key: string): string => key.replaceAll('_', ' ');

	function metric(value: string | number): string {
		return typeof value === 'number' ? String(value) : value;
	}
</script>

<section class="rounded-xl border border-surface-700 bg-surface-900" data-testid="screener-results">
	<div class="flex items-center gap-2 border-b border-surface-700 px-4 py-2.5">
		<h3 class="text-sm font-semibold text-slate-800">Results</h3>
		{#if result}
			<span
				class="rounded-full border px-2 py-0.5 text-[10px] {result.status === 'succeeded'
					? 'border-profit/40 bg-profit/10 text-profit'
					: 'border-warn/40 bg-warn/10 text-warn'}"
				data-testid="screener-status"
			>
				{result.status}
			</span>
			<span class="font-mono text-[10px] text-slate-500">
				{result.rows.length} row{result.rows.length === 1 ? '' : 's'}
			</span>
		{/if}
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
			<span data-testid="provenance-result-id">result {result.result_id}</span>
			<span data-testid="provenance-job-id">job {result.job_id}</span>
			<span data-testid="provenance-as-of">as of {result.as_of}</span>
			<span data-testid="provenance-definition-id">definition {result.definition_id}</span>
			<span data-testid="provenance-definition-hash" title={result.definition_hash}>
				hash {shortHash(result.definition_hash)}
			</span>
			{#if definitionName}
				<span data-testid="provenance-definition-name">{definitionName}</span>
			{/if}
			{#if rankMetric}
				<span>ranked by {rankMetric}</span>
			{/if}
			{#if dataWindow}
				<span>window {dataWindow}</span>
			{/if}
		</div>
		{#if result.rows.length}
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
						{#each result.rows as row, index (`${row.symbol}:${index}`)}
							<tr
								class="border-t border-surface-700"
								data-testid="screener-row"
								data-symbol={row.symbol}
							>
								<td class="px-4 py-1.5 text-slate-500">{row.rank ?? index + 1}</td>
								<td class="px-4 py-1.5 font-semibold text-slate-800">{row.symbol}</td>
								{#each metricKeys as key (key)}
									<td class="px-4 py-1.5 text-right text-slate-700">
										{row.metrics[key] === undefined ? '—' : metric(row.metrics[key])}
									</td>
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
				hint={running
					? 'Screening the as-of universe…'
					: 'Author or load a definition, then run it against an as-of date.'}
			/>
		</div>
	{/if}
</section>
