<script lang="ts">
	import { page } from '$app/state';
	import EmptyState from '$lib/components/common/EmptyState.svelte';
	import LineageGraph from '$lib/components/state/LineageGraph.svelte';
	import { getStrategy } from '$lib/api';
	import { money, pct, signedPct } from '$lib/format';
	import type { Strategy } from '$lib/types';

	const workspaceId = $derived(page.params.workspace ?? '');
	const strategyId = $derived(page.params.id ?? '');

	let strategy = $state<Strategy | null>(null);
	let loaded = $state(false);

	$effect(() => {
		let cancelled = false;
		getStrategy(workspaceId, strategyId).then((item) => {
			if (!cancelled) {
				strategy = item;
				loaded = true;
			}
		});
		return () => {
			cancelled = true;
		};
	});
</script>

<svelte:head><title>{strategy?.name ?? 'Strategy'} — Thesis</title></svelte:head>

<div class="mx-auto max-w-6xl space-y-4">
	<a href="/w/{workspaceId}/strategies" class="text-xs text-slate-500 hover:text-accent">← Strategies</a>

	{#if loaded && !strategy}
		<EmptyState title="Strategy not found" hint="It may live in a different workspace." />
	{:else if strategy}
		{@const s = strategy}
		<header class="flex flex-wrap items-center gap-3">
			<div>
				<h1 class="text-xl font-semibold text-white">{s.name}</h1>
				<p class="font-mono text-xs text-slate-500">
					{s.symbol} · {s.status} · capital {money(s.capital)}
				</p>
			</div>
			<div class="ml-auto flex items-baseline gap-4 font-mono text-sm">
				<span class={s.pnl >= 0 ? 'text-profit' : 'text-loss'}>{signedPct(s.return_pct)}</span>
				<span class="text-slate-500 text-xs">win {pct(s.win_rate, 0)}</span>
			</div>
		</header>

		<section class="rounded-xl border border-surface-700 bg-surface-900">
			<h2 class="border-b border-surface-700 px-4 py-2.5 text-sm font-semibold text-slate-200">Code</h2>
			<pre
				class="overflow-x-auto p-4 font-mono text-xs leading-relaxed text-slate-300"
				data-testid="strategy-code">{s.code}</pre>
		</section>

		<section class="space-y-3">
			<h2 class="text-sm font-semibold text-slate-200">Lineage</h2>
			<LineageGraph
				lineage={{
					strategy_id: s.id,
					strategy_name: s.name,
					nodes: s.versions.map((v) => ({
						id: `${s.id}@${v.version}`,
						version: v.version,
						created_at: v.created_at,
						note: v.note
					})),
					edges: s.versions
						.filter((v) => v.parent_version)
						.map((v) => ({
							from: `${s.id}@${v.parent_version}`,
							to: `${s.id}@${v.version}`,
							relation: 'adapted-from' as const
						}))
				}}
			/>
		</section>
	{:else}
		<div class="h-40 animate-pulse rounded-xl border border-surface-700 bg-surface-900"></div>
	{/if}
</div>
