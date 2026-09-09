<script lang="ts">
	import EmptyState from '$lib/components/common/EmptyState.svelte';
	import MetricCard from '$lib/components/common/MetricCard.svelte';
	import { listBacktests } from '$lib/api';
	import { shortDate } from '$lib/format';
	import type { BacktestSummaryRow } from '$lib/types';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();

	const workspaceId = $derived(data.workspaceId);

	let backtests = $state<BacktestSummaryRow[]>([]);
	let strategyFilter = $state('all');
	let onlySplit = $state(false);

	$effect(() => {
		let cancelled = false;
		listBacktests(workspaceId).then((items) => {
			if (!cancelled) backtests = items;
		});
		return () => {
			cancelled = true;
		};
	});

	const strategyIds = $derived([...new Set(backtests.map((b) => b.strategy_id))]);
	const filtered = $derived(
		backtests.filter(
			(b) =>
				(strategyFilter === 'all' || b.strategy_id === strategyFilter) &&
				(!onlySplit || b.split_declared)
		)
	);

	const summary = $derived.by(() => {
		if (!backtests.length) return { count: 0, best: 0, avgSharpe: 0 };
		const best = Math.max(...backtests.map((b) => b.total_return));
		const avgSharpe = backtests.reduce((a, b) => a + b.sharpe, 0) / backtests.length;
		return { count: backtests.length, best, avgSharpe };
	});
</script>

<svelte:head><title>Backtests — Thesis</title></svelte:head>

<div class="mx-auto max-w-6xl space-y-4">
	<header class="flex flex-wrap items-center gap-3">
		<div>
			<h1 class="text-xl font-semibold text-white">Backtests</h1>
			<p class="text-sm text-slate-500">Run history and declared-split results.</p>
		</div>
		<button
			type="button"
			class="ml-auto rounded-lg border border-accent/40 bg-accent/15 px-3 py-1.5 text-xs font-semibold text-accent hover:bg-accent/25"
		>
			＋ Run backtest
		</button>
	</header>

	<div class="grid grid-cols-3 gap-3">
		<MetricCard label="Runs" value={summary.count} />
		<MetricCard label="Best return" value={summary.best} format="signedPct" />
		<MetricCard label="Average Sharpe" value={summary.avgSharpe} format="ratio" />
	</div>

	<div class="flex flex-wrap items-center gap-2">
		<select
			class="rounded-lg border border-white/10 bg-surface-900 px-2 py-1.5 text-xs text-slate-300 outline-none"
			bind:value={strategyFilter}
			aria-label="Filter by strategy"
		>
			<option value="all">All strategies</option>
			{#each strategyIds as id (id)}
				<option value={id}>{id}</option>
			{/each}
		</select>
		<label class="flex items-center gap-1.5 text-xs text-slate-400">
			<input type="checkbox" class="accent-cyan-400" bind:checked={onlySplit} />
			split declared only
		</label>
	</div>

	{#if filtered.length === 0}
		<EmptyState title="No backtests" hint="Runs submitted from chats will appear here." />
	{:else}
		<div class="overflow-hidden rounded-xl border border-white/5 bg-surface-850/40" data-testid="backtest-history">
			<table class="w-full border-collapse font-mono text-xs">
				<thead>
					<tr class="bg-surface-900/80 text-left text-[10px] tracking-wider text-slate-500 uppercase">
						<th class="px-4 py-2">Run</th>
						<th class="px-4 py-2">Strategy</th>
						<th class="px-4 py-2">Period</th>
						<th class="px-4 py-2 text-right">Return</th>
						<th class="px-4 py-2 text-right">Sharpe</th>
						<th class="px-4 py-2 text-right">Max DD</th>
						<th class="px-4 py-2 text-center">Split</th>
					</tr>
				</thead>
				<tbody>
					{#each filtered as backtest (backtest.id)}
						<tr class="border-t border-white/5 hover:bg-surface-850/60">
							<td class="px-4 py-2">
								<a href="/w/{workspaceId}/backtests/{backtest.id}" class="text-accent hover:underline">
									{backtest.name}
								</a>
							</td>
							<td class="px-4 py-2 text-slate-400">{backtest.strategy_id}</td>
							<td class="px-4 py-2 text-slate-500">
								{shortDate(backtest.period_start)} → {shortDate(backtest.period_end)}
							</td>
							<td class="px-4 py-2 text-right {backtest.total_return >= 0 ? 'text-profit' : 'text-loss'}">
								{(backtest.total_return * 100).toFixed(1)}%
							</td>
							<td class="px-4 py-2 text-right text-slate-300">{backtest.sharpe.toFixed(2)}</td>
							<td class="px-4 py-2 text-right text-loss">{(backtest.max_drawdown * 100).toFixed(1)}%</td>
							<td class="px-4 py-2 text-center">
								{#if backtest.split_declared}
									<span class="rounded-full border border-accent/40 px-2 py-0.5 text-[10px] text-accent">declared</span>
								{:else}
									<span class="rounded-full border border-surface-600 px-2 py-0.5 text-[10px] text-slate-500">not declared</span>
								{/if}
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}
</div>
