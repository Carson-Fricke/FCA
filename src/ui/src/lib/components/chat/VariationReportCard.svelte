<script lang="ts">
	import ParameterBarChart from '$lib/components/charts/ParameterBarChart.svelte';
	import { isSucceededState, parameterCategories, runStateTone } from '$lib/components/charts/parameter-bar';
	import type { VariationReportData } from '$lib/transcript';

	/**
	 * The deterministic parameter-variation report card: one chart per varied
	 * parameter, the center configuration marked as the selected baseline, and
	 * explicit gaps for metrics a run did not record. Data is the decoded
	 * variation artifact; nothing is inferred beyond the payload.
	 */
	let { data }: { data: VariationReportData; workspaceId?: string } = $props();

	const center = $derived(data.center);
	const points = $derived(data.points ?? []);
	const parameters = $derived(data.parameters ?? []);
	const charts = $derived.by(() =>
		parameters.map((name) => ({ name, categories: parameterCategories(name, center, points) }))
	);
	const succeededCount = $derived(points.filter((point) => isSucceededState(point.state)).length);
</script>

<section
	class="overflow-hidden rounded-xl border border-surface-700 bg-surface-900 shadow-sm"
	data-testid="variation-report-card"
>
	<header class="border-b border-surface-700 bg-surface-950/40 p-3.5">
		<div class="flex flex-wrap items-center justify-between gap-2">
			<h3 class="text-sm font-semibold text-slate-800">Parameter Variation</h3>
			<div class="flex flex-wrap items-center gap-2">
				{#if succeededCount < points.length}
					<span
						class="rounded-full border border-warn/40 bg-warn/10 px-2.5 py-0.5 font-mono text-[10px] font-semibold uppercase text-warn"
						data-testid="variation-state-summary"
					>
						{succeededCount}/{points.length} points succeeded
					</span>
				{/if}
				<span
					class="rounded-full border border-accent/40 bg-accent/10 px-2.5 py-0.5 font-mono text-[10px] font-semibold uppercase text-accent"
					data-testid="variation-summary"
				>
					{parameters.length} {parameters.length === 1 ? 'parameter' : 'parameters'} ·
					{points.length} {points.length === 1 ? 'point' : 'points'}
				</span>
			</div>
		</div>
		{#if center}
			<div
				class="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-700"
				data-testid="variation-center"
			>
				<span class="font-semibold text-slate-500">Center:</span>
				<span
					class="font-mono text-[10px] font-semibold uppercase {runStateTone(center.state)}"
					data-testid="variation-center-state"
				>
					{center.state ?? 'unknown'}
				</span>
				{#if center.jobId}
					<span class="font-mono text-[10px] text-slate-500">job {center.jobId}</span>
				{/if}
				{#each Object.entries(center.parameters) as [key, value] (key)}
					<span class="rounded-md border border-surface-700 bg-surface-950/40 px-1.5 py-0.5 font-mono text-[10px]">
						<span class="text-slate-500">{key}</span>
						<span class="ml-1 font-semibold text-slate-800">{String(value)}</span>
					</span>
				{/each}
			</div>
		{/if}
		{#if data.stability}
			<p class="mt-2 text-xs leading-snug text-slate-600" data-testid="variation-stability">
				{data.stability}
			</p>
		{/if}
	</header>

	{#if charts.length}
		<div class="space-y-3 p-3.5" data-testid="variation-charts">
			{#each charts as chart (chart.name)}
				<ParameterBarChart
					title={chart.name}
					categories={chart.categories}
					testId={`variation-chart-${chart.name}`}
				/>
			{/each}
		</div>
	{:else}
		<p class="p-3.5 text-xs text-slate-500" data-testid="variation-empty">
			No varied parameters were recorded for this run.
		</p>
	{/if}
</section>
