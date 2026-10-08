<script lang="ts">
	import { page } from '$app/state';
	import SplitEquityView from '$lib/components/backtest/SplitEquityView.svelte';
	import EmptyState from '$lib/components/common/EmptyState.svelte';
	import { getBacktest } from '$lib/api';
	import { shortHash } from '$lib/format';
	import type { BacktestDetail } from '$lib/types';

	const workspaceId = $derived(page.params.workspace ?? '');
	const jobId = $derived(page.params.id ?? '');

	let backtest = $state<BacktestDetail | null>(null);
	let loadError = $state('');

	$effect(() => {
		let cancelled = false;
		loadError = '';
		getBacktest(workspaceId, jobId)
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

	/** Human-readable byte size for artifact rows. */
	function humanBytes(size: number): string {
		if (!Number.isFinite(size) || size < 0) return '—';
		if (size < 1024) return `${size} B`;
		const units = ['KB', 'MB', 'GB', 'TB'];
		let value = size / 1024;
		let unit = 0;
		while (value >= 1024 && unit < units.length - 1) {
			value /= 1024;
			unit += 1;
		}
		return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[unit]}`;
	}
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

		{#if backtest.chat_id}
			<a
				href="/w/{workspaceId}/conversations?chat={backtest.chat_id}"
				class="inline-flex items-center gap-1.5 text-xs text-accent hover:underline"
				data-testid="backtest-started-in-chat"
				title={backtest.execution_id
					? `execution ${backtest.execution_id}`
					: 'Open the chat that started this run'}
			>
				← Started in chat
			</a>
		{/if}

		<SplitEquityView {backtest} />

		{#if backtest.artifacts?.length}
			<section
				class="overflow-hidden rounded-xl border border-surface-700 bg-surface-900"
				data-testid="backtest-artifacts"
			>
				<header
					class="flex flex-wrap items-baseline gap-2 border-b border-surface-700 px-4 py-2.5"
				>
					<h2 class="text-xs font-semibold tracking-wide text-slate-700 uppercase">Artifacts</h2>
					<span class="font-mono text-[10px] text-slate-500">
						{backtest.artifacts.length} published · manifest declares
						{backtest.manifest.artifact_count}
					</span>
				</header>
				<table class="w-full border-collapse font-mono text-xs">
					<thead>
						<tr class="bg-surface-900 text-left text-[10px] tracking-wider text-slate-500 uppercase">
							<th class="px-4 py-2">Name</th>
							<th class="px-4 py-2 text-right">Size</th>
							<th class="px-4 py-2">SHA-256</th>
							<th class="px-4 py-2">Object</th>
						</tr>
					</thead>
					<tbody>
						{#each backtest.artifacts as artifact (artifact.object_path ?? artifact.name)}
							<tr class="border-t border-surface-700 hover:bg-surface-900">
								<td class="px-4 py-2 text-slate-700">{artifact.name}</td>
								<td class="px-4 py-2 text-right text-slate-500">{humanBytes(artifact.size)}</td>
								<td class="px-4 py-2 text-slate-500">{shortHash(artifact.sha256 ?? null)}</td>
								<td class="px-4 py-2 text-slate-500">{artifact.object_path}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</section>
		{/if}
	{:else}
		<div class="h-64 animate-pulse rounded-xl border border-surface-700 bg-surface-900"></div>
	{/if}
</div>
