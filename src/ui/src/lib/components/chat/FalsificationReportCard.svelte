<script lang="ts">
	import HistogramChart from '$lib/components/charts/HistogramChart.svelte';
	import TimeSeriesChart from '$lib/components/charts/TimeSeriesChart.svelte';
	import type { FalsificationReportData } from '$lib/transcript';

	let { data }: { data: FalsificationReportData; workspaceId?: string } = $props();
	type TabId = 'scorecard' | 'controls' | 'evidence';
	let activeTab = $state<TabId>('scorecard');

	const trials = $derived(data.trials ?? []);
	const controls = $derived(data.controls ?? []);
	const distributions = $derived(Object.entries(data.distributions ?? {}));
	const series = $derived(data.series ?? []);
	const hypothesis = $derived(data.hypothesis ?? {});
	const refuted = $derived(trials.filter((trial) => trial.falsified).length);
	const passed = $derived(
		trials.filter((trial) => !trial.falsified && trial.observed !== null).length
	);
	const total = $derived(trials.length > 0 ? trials.length : (data.totalCount ?? 0));
	const survived = $derived(
		trials.length > 0
			? refuted === 0 && passed === total
			: (data.survived ?? (total > 0 && passed === total))
	);

	function label(value: string): string {
		return value.replace(/[_-]/g, ' ').replace(/\b\w/g, (character) => character.toUpperCase());
	}

	function number(value: number | null | undefined): string {
		if (value == null || !Number.isFinite(value)) return 'not observed';
		if (value !== 0 && Math.abs(value) < 0.0001) return value.toExponential(2);
		if (Math.abs(value) < 1) return value.toFixed(4);
		return value.toFixed(2);
	}

	function gauge(observed: number | null, threshold: number): { observed: number; threshold: number } {
		const value = observed ?? threshold;
		const distance = Math.abs(value - threshold) || Math.max(Math.abs(threshold), 1);
		const minimum = Math.min(value, threshold) - distance * 0.4;
		const span = Math.max(Math.max(value, threshold) + distance * 0.4 - minimum, Number.EPSILON);
		return {
			observed: Math.max(4, Math.min(96, ((value - minimum) / span) * 100)),
			threshold: Math.max(4, Math.min(96, ((threshold - minimum) / span) * 100))
		};
	}
</script>

<section
	class="overflow-hidden rounded-xl border border-surface-700 bg-surface-900 shadow-sm"
	data-testid="falsification-report-card"
>
	<header class="border-b border-surface-700 bg-surface-950/40 p-3.5">
		<div class="flex flex-wrap items-center justify-between gap-2">
			<h3 class="text-sm font-semibold text-slate-800">Falsification Trials Report</h3>
			<span
				class="rounded-full border px-2.5 py-0.5 font-mono text-[10px] font-semibold uppercase {survived
					? 'border-profit/40 bg-profit/10 text-profit'
					: refuted > 0
						? 'border-loss/40 bg-loss/10 text-loss'
						: 'border-warn/40 bg-warn/10 text-warn'}"
				data-testid="falsification-badge"
			>
				{survived
					? `${passed}/${total} Trials Survived`
					: refuted > 0
						? `${refuted}/${total} Refuted`
						: `${passed}/${total} Observed`}
			</span>
		</div>
		{#if hypothesis.claim}
			<p class="mt-2 text-xs text-slate-700" data-testid="falsification-claim">
				<span class="font-semibold text-slate-500">Claim:</span> {hypothesis.claim}
			</p>
		{/if}
		{#if hypothesis.horizon}
			<p class="mt-1 font-mono text-[10px] text-slate-500">{hypothesis.horizon}</p>
		{/if}
		{#if trials.some((trial) => trial.observed !== null)}
			<div class="mt-3 flex flex-wrap gap-1.5" data-testid="metric-chips">
				{#each trials.filter((trial) => trial.observed !== null).slice(0, 6) as trial (trial.testId)}
					<span class="rounded-md border border-surface-700 bg-surface-900 px-2 py-1 font-mono text-[10px]">
						<span class="text-slate-500">{label(trial.observable)}</span>
						<span class="ml-1 font-semibold text-slate-800">{number(trial.observed)}</span>
					</span>
				{/each}
			</div>
		{/if}
	</header>

	<nav class="flex overflow-x-auto border-b border-surface-700 px-3 text-xs" aria-label="Falsification report views">
		{#each [
			{ id: 'scorecard', text: 'Scorecard' },
			{ id: 'controls', text: 'Null & controls' },
			{ id: 'evidence', text: 'Dynamics & evidence' }
		] as tab (tab.id)}
			<button
				type="button"
				class="border-b-2 px-3 py-2.5 font-medium whitespace-nowrap transition {activeTab === tab.id
					? 'border-accent text-accent'
					: 'border-transparent text-slate-500 hover:text-slate-700'}"
				onclick={() => (activeTab = tab.id as TabId)}
				data-testid="tab-{tab.id}"
			>
				{tab.text}
			</button>
		{/each}
	</nav>

	{#if activeTab === 'scorecard'}
		<div class="space-y-3 p-3.5" data-testid="scorecard-view">
			{#each trials as trial (trial.testId)}
				{@const position = gauge(trial.observed, trial.threshold)}
				<div
					class="rounded-lg border border-surface-700 bg-surface-950/30 p-3"
					data-testid="trial-row-{trial.testId}"
				>
					<div class="flex items-start justify-between gap-3">
						<div>
							<p class="font-mono text-xs font-semibold text-slate-800">{trial.testId}</p>
							<p class="mt-0.5 text-[11px] text-slate-500">{trial.description ?? label(trial.observable)}</p>
						</div>
						<span
							class="rounded-full px-2 py-0.5 font-mono text-[10px] font-semibold {trial.falsified
								? 'bg-loss/15 text-loss'
								: trial.observed === null
									? 'bg-warn/15 text-warn'
									: 'bg-profit/15 text-profit'}"
							data-testid="trial-status"
						>
							{trial.falsified ? 'REFUTED' : trial.observed === null ? 'NO EVIDENCE' : 'SURVIVED'}
						</span>
					</div>
					<div class="relative mt-3 h-3 overflow-hidden rounded-full bg-surface-800" aria-hidden="true">
						<div class="absolute inset-y-0 left-0 bg-profit/20" style="width: {position.threshold}%"></div>
						<div class="absolute inset-y-0 border-r border-dashed border-loss" style="left: {position.threshold}%"></div>
						{#if trial.observed !== null}
							<div class="absolute inset-y-0 w-1 -translate-x-1/2 rounded bg-accent" style="left: {position.observed}%"></div>
						{/if}
					</div>
					<div class="mt-1 flex justify-between font-mono text-[10px]">
						{#if trial.observed === null}
							<span class="text-warn" data-testid="trial-observed">no observation recorded</span>
						{:else}
							<span
								class={trial.falsified ? 'text-loss' : 'text-profit'}
								data-testid="trial-observed"
							>
								observed {number(trial.observed)}
							</span>
						{/if}
						<span class="text-slate-500">falsifies when {trial.condition} {number(trial.threshold)}</span>
					</div>
					{#if trial.detail}
						<p class="mt-1.5 text-[11px] leading-snug text-slate-600" data-testid="trial-detail">
							{trial.detail}
						</p>
					{/if}
					{#if trial.runState || trial.jobId}
						<p class="mt-1 font-mono text-[10px] text-slate-500" data-testid="trial-run">
							{#if trial.runState}<span class="font-semibold text-slate-600 uppercase">{trial.runState}</span>{/if}
							{#if trial.runState && trial.jobId}<span class="mx-1">·</span>{/if}
							{#if trial.jobId}<span>job {trial.jobId}</span>{/if}
						</p>
					{/if}
				</div>
			{/each}
		</div>
	{:else if activeTab === 'controls'}
		<div class="space-y-4 p-3.5" data-testid="controls-view">
			{#if distributions.length}
				{#each distributions as [name, values] (name)}
					<HistogramChart {values} height={190} bins={13} label={label(name)} />
				{/each}
			{:else}
				<p class="rounded-lg border border-surface-700 bg-surface-950/30 p-3 text-xs text-slate-500">
					No empirical null-distribution samples were recorded for this run. The control specifications remain available below.
				</p>
			{/if}
			<ul class="space-y-2">
				{#each controls as control (control.controlId)}
					<li class="rounded-lg border border-surface-700 p-3 text-xs">
						<p class="font-mono font-semibold text-slate-800">{control.controlId}</p>
						<p class="mt-1 text-slate-600">{control.description}</p>
						{#if control.expectation}<p class="mt-1 text-[10px] text-slate-500">Expected: {control.expectation}</p>{/if}
					</li>
				{/each}
			</ul>
		</div>
	{:else}
		<div class="space-y-4 p-3.5" data-testid="evidence-view">
			{#if series.length}
				{#each series as item (item.name)}
					<TimeSeriesChart title={item.name} data={item.data} render="line" height={220} />
				{/each}
			{:else}
				<p class="rounded-lg border border-surface-700 bg-surface-950/30 p-3 text-xs text-slate-500">
					No time-series dynamics were recorded for this run. Showing the actual scalar evidence produced by the model instead.
				</p>
			{/if}
			<div class="grid gap-2 sm:grid-cols-2">
				{#each trials as trial (trial.testId)}
					<div class="rounded-lg border border-surface-700 bg-surface-950/30 p-3">
						<p class="text-[10px] font-semibold tracking-wide text-slate-500 uppercase">{label(trial.observable)}</p>
						<p
							class="mt-1 font-mono text-sm font-semibold {trial.falsified
								? 'text-loss'
								: trial.observed === null
									? 'text-warn'
									: 'text-profit'}"
						>
							{number(trial.observed)}
						</p>
						<p class="mt-1 font-mono text-[10px] text-slate-500">threshold {trial.condition} {number(trial.threshold)}</p>
					</div>
				{/each}
			</div>
		</div>
	{/if}
</section>
