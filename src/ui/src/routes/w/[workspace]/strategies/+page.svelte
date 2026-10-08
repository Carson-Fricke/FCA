<script lang="ts">
	import EmptyState from '$lib/components/common/EmptyState.svelte';
	import CreateStrategyDialog from '$lib/components/strategies/CreateStrategyDialog.svelte';
	import { money, pct, signedPct } from '$lib/format';
	import { createStrategy, listStrategies } from '$lib/api';
	import { notify } from '$lib/workspace.svelte';
	import type { Strategy, StrategyStatus } from '$lib/types';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();

	const workspaceId = $derived(data.workspaceId);

	let strategies = $state<Strategy[]>([]);
	let view = $state<'grid' | 'list'>('grid');
	let statusFilter = $state<'all' | StrategyStatus>('all');
	let createOpen = $state(false);

	$effect(() => {
		let cancelled = false;
		listStrategies(workspaceId).then((items) => {
			if (!cancelled) strategies = items;
		});
		return () => {
			cancelled = true;
		};
	});

	const filtered = $derived(
		statusFilter === 'all' ? strategies : strategies.filter((s) => s.status === statusFilter)
	);

	const STATUS_TONE: Record<StrategyStatus, string> = {
		live: 'border-profit/40 text-profit',
		paper: 'border-accent/40 text-accent',
		paused: 'border-warn/40 text-warn',
		draft: 'border-surface-600 text-slate-500'
	};
</script>

<svelte:head><title>Strategies — Thesis</title></svelte:head>

<div class="mx-auto max-w-6xl space-y-4">
	<header class="flex flex-wrap items-center gap-3">
		<div>
			<h1 class="text-xl font-semibold text-slate-800">Strategies</h1>
			<p class="text-sm text-slate-500">Registry with status filters and version lineage.</p>
		</div>
		<div class="ml-auto flex items-center gap-2">
			<div class="flex overflow-hidden rounded-lg border border-surface-600">
				<button
					type="button"
					class="px-3 py-1.5 text-xs {view === 'grid' ? 'bg-accent/15 text-accent' : 'text-slate-500'}"
					onclick={() => (view = 'grid')}
				>
					Grid
				</button>
				<button
					type="button"
					class="px-3 py-1.5 text-xs {view === 'list' ? 'bg-accent/15 text-accent' : 'text-slate-500'}"
					onclick={() => (view = 'list')}
				>
					List
				</button>
			</div>
			<select
				class="rounded-lg border border-surface-600 bg-surface-900 px-2 py-1.5 text-xs text-slate-700 outline-none"
				bind:value={statusFilter}
				aria-label="Filter by status"
			>
				<option value="all">All statuses</option>
				<option value="live">Live</option>
				<option value="paper">Paper</option>
				<option value="paused">Paused</option>
				<option value="draft">Draft</option>
			</select>
			<button
				type="button"
				class="rounded-lg border border-accent/40 bg-accent/15 px-3 py-1.5 text-xs font-semibold text-accent hover:bg-accent/25"
				onclick={() => (createOpen = true)}
				data-testid="new-strategy"
			>
				＋ New strategy
			</button>
		</div>
	</header>

	{#if filtered.length === 0}
		<EmptyState title="No strategies match" hint="Adjust the status filter or start a chat to draft one." />
	{:else if view === 'grid'}
		<ul class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
			{#each filtered as strategy (strategy.id)}
				<li>
					<a
						href="/w/{workspaceId}/strategies/{strategy.id}"
						class="block rounded-xl border border-surface-700 bg-surface-900 p-4 transition hover:border-accent/40"
						data-testid="strategy-card"
					>
						<div class="flex items-center gap-2">
							<h2 class="truncate text-sm font-semibold text-slate-800">{strategy.name}</h2>
							<span class="rounded-full border px-2 py-0.5 font-mono text-[10px] {STATUS_TONE[strategy.status]}">
								{strategy.status}
							</span>
						</div>
						<p class="mt-0.5 font-mono text-[11px] text-slate-500">{strategy.symbol} · {strategy.versions.at(-1)?.version ?? '—'}</p>
						<p class="mt-2 truncate text-xs text-slate-500">{strategy.description}</p>
						<div class="mt-3 flex items-baseline gap-4 font-mono text-xs">
							<span class={strategy.return_pct == null ? 'text-slate-500' : strategy.return_pct >= 0 ? 'text-profit' : 'text-loss'}>{signedPct(strategy.return_pct)}</span>
							<span class="text-slate-500">win {pct(strategy.win_rate, 0)}</span>
							<span class="ml-auto text-slate-500">{money(strategy.capital)}</span>
						</div>
					</a>
				</li>
			{/each}
		</ul>
	{:else}
		<div class="overflow-hidden rounded-xl border border-surface-700">
			<table class="w-full border-collapse text-sm">
				<thead>
					<tr class="bg-surface-900 text-left text-[10px] tracking-wider text-slate-500 uppercase">
						<th class="px-4 py-2">Strategy</th>
						<th class="px-4 py-2">Symbol</th>
						<th class="px-4 py-2">Status</th>
						<th class="px-4 py-2 text-right">Return</th>
						<th class="px-4 py-2 text-right">Win rate</th>
						<th class="px-4 py-2 text-right">Capital</th>
					</tr>
				</thead>
				<tbody>
					{#each filtered as strategy (strategy.id)}
						<tr class="border-t border-surface-700 hover:bg-surface-900">
							<td class="px-4 py-2">
								<a href="/w/{workspaceId}/strategies/{strategy.id}" class="text-slate-800 hover:text-accent">
									{strategy.name}
								</a>
							</td>
							<td class="px-4 py-2 font-mono text-xs text-slate-500">{strategy.symbol}</td>
							<td class="px-4 py-2">
								<span class="rounded-full border px-2 py-0.5 font-mono text-[10px] {STATUS_TONE[strategy.status]}">
									{strategy.status}
								</span>
							</td>
							<td class="px-4 py-2 text-right font-mono text-xs {strategy.return_pct == null ? 'text-slate-500' : strategy.return_pct >= 0 ? 'text-profit' : 'text-loss'}">
								{signedPct(strategy.return_pct)}
							</td>
							<td class="px-4 py-2 text-right font-mono text-xs text-slate-500">{pct(strategy.win_rate, 0)}</td>
							<td class="px-4 py-2 text-right font-mono text-xs text-slate-500">{money(strategy.capital)}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}
</div>

{#if createOpen}
	<CreateStrategyDialog
		onClose={() => (createOpen = false)}
		onCreate={async (input) => {
			createOpen = false;
			try {
				const created = await createStrategy(workspaceId, input);
				strategies = [created, ...strategies];
				notify(`Strategy “${created.name}” created`);
			} catch (error) {
				notify(
					`Strategy creation failed: ${error instanceof Error ? error.message : error}`,
					true
				);
			}
		}}
	/>
{/if}
