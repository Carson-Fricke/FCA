<script lang="ts">
	import { page } from '$app/state';
	import SplitEquityView from '$lib/components/backtest/SplitEquityView.svelte';
	import EmptyState from '$lib/components/common/EmptyState.svelte';
	import { getBacktest } from '$lib/api';
	import type { BacktestDetail } from '$lib/types';

	const workspaceId = $derived(page.params.workspace ?? '');
	const backtestId = $derived(page.params.id ?? '');

	let backtest = $state<BacktestDetail | null>(null);
	let loadError = $state('');

	$effect(() => {
		let cancelled = false;
		loadError = '';
		getBacktest(workspaceId, backtestId)
			.then((item) => {
				if (!cancelled) backtest = item;
			})
			.catch((error: unknown) => {
				if (!cancelled) loadError = error instanceof Error ? error.message : 'Load failed';
			});
		return () => {
			cancelled = true;
		};
	});
</script>

<svelte:head><title>Backtest detail — Thesis</title></svelte:head>

<div class="mx-auto max-w-6xl space-y-4">
	<a href="/w/{workspaceId}/backtests" class="text-xs text-slate-500 hover:text-accent">← Backtests</a>

	{#if loadError}
		<EmptyState title="Backtest unavailable" hint={loadError} />
	{:else if backtest}
		<header class="flex flex-wrap items-baseline gap-3">
			<h1 class="text-xl font-semibold text-slate-800">{backtest.name}</h1>
			<span class="font-mono text-xs text-slate-500">
				{backtest.period_start} → {backtest.period_end} · initial ${backtest.initial_capital.toLocaleString('en-US')} · final
				<span class={backtest.net_pnl >= 0 ? 'text-profit' : 'text-loss'}>
					${backtest.final_capital.toLocaleString('en-US')}
				</span>
				({backtest.net_pnl >= 0 ? '+' : ''}{backtest.net_pnl.toLocaleString('en-US')})
			</span>
		</header>

		<SplitEquityView {backtest} />
	{:else}
		<div class="h-64 animate-pulse rounded-xl border border-surface-700 bg-surface-900"></div>
	{/if}
</div>
