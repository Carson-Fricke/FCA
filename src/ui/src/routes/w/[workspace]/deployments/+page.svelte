<script lang="ts">
	import EmptyState from '$lib/components/common/EmptyState.svelte';
	import StatusSurfaceView from '$lib/components/common/StatusSurfaceView.svelte';
	import { getPortfolio, getRiskDecisions, listDeployments, listOrders } from '$lib/api';
	import { relativeTime } from '$lib/format';
	import type {
		DeploymentCard,
		OrderRecord,
		PortfolioSummary,
		RiskDecisionRecord,
		StatusSurface
	} from '$lib/types';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();

	const workspaceId = $derived(data.workspaceId);

	let deployments = $state<DeploymentCard[]>([]);
	let orders = $state<OrderRecord[]>([]);
	let portfolio = $state<StatusSurface<PortfolioSummary>>({ state: 'unavailable', value: null, reason: null });
	let risk = $state<Record<string, StatusSurface<RiskDecisionRecord[]>>>({});

	$effect(() => {
		let cancelled = false;
		listDeployments(workspaceId).then((items) => {
			if (!cancelled) deployments = items;
		});
		listOrders(workspaceId).then((items) => {
			if (!cancelled) orders = items;
		});
		getPortfolio(workspaceId).then((surface) => {
			if (!cancelled) portfolio = surface;
		});
		return () => {
			cancelled = true;
		};
	});

	$effect(() => {
		const refs = deployments.map((d) => d.request_ref).filter((r): r is string => Boolean(r));
		for (const ref of refs) {
			if (risk[ref]) continue;
			getRiskDecisions(ref).then((surface) => {
				risk = { ...risk, [ref]: surface };
			});
		}
	});

	const MODE_BADGE: Record<string, string> = {
		live: 'border-loss/50 text-loss',
		paper: 'border-accent/40 text-accent'
	};
</script>

<svelte:head><title>Deployments — Thesis</title></svelte:head>

<div class="mx-auto max-w-6xl space-y-5">
	<header>
		<h1 class="text-xl font-semibold text-white">Deployments</h1>
		<p class="text-sm text-slate-500">Paper/live monitoring: health, capital, P&amp;L, orders, risk.</p>
	</header>

	{#if deployments.length === 0}
		<EmptyState title="No deployments" hint="Strategies reach deployment through structured consent." />
	{:else}
		<ul class="grid gap-3 lg:grid-cols-2">
			{#each deployments as deployment (deployment.id)}
				<li class="rounded-xl border border-white/5 bg-surface-850/60 p-4" data-testid="deployment-card">
					<div class="flex items-center gap-2">
						<h2 class="truncate text-sm font-semibold text-slate-100">{deployment.strategy_name}</h2>
						<span class="rounded-full border px-2 py-0.5 font-mono text-[10px] uppercase {MODE_BADGE[deployment.mode]}">
							{deployment.mode}
						</span>
						<span class="ml-auto flex items-center gap-1.5 font-mono text-[10px] {deployment.health === 'healthy' ? 'text-profit' : 'text-warn'}">
							<span
								class="h-1.5 w-1.5 rounded-full {deployment.health === 'healthy' ? 'bg-profit' : 'bg-warn'}"
								aria-hidden="true"
							></span>
							{deployment.health}
						</span>
					</div>
					<p class="mt-0.5 font-mono text-[11px] text-slate-500">
						{deployment.state} · since {deployment.since.slice(0, 16).replace('T', ' ')}
					</p>
					<div class="mt-3 grid grid-cols-3 gap-2 font-mono text-xs">
						<div>
							<div class="text-[10px] tracking-wider text-slate-600 uppercase">Capital</div>
							<div class="text-slate-200">${deployment.allocated_capital.toLocaleString('en-US')}</div>
						</div>
						<div>
							<div class="text-[10px] tracking-wider text-slate-600 uppercase">Unrealized</div>
							<div class={deployment.unrealized_pnl >= 0 ? 'text-profit' : 'text-loss'}>
								{deployment.unrealized_pnl >= 0 ? '+' : ''}${Math.abs(deployment.unrealized_pnl).toLocaleString('en-US')}
							</div>
						</div>
						<div>
							<div class="text-[10px] tracking-wider text-slate-600 uppercase">Realized</div>
							<div class={deployment.realized_pnl >= 0 ? 'text-profit' : 'text-loss'}>
								{deployment.realized_pnl >= 0 ? '+' : ''}${Math.abs(deployment.realized_pnl).toLocaleString('en-US')}
							</div>
						</div>
					</div>
					{#if deployment.request_ref && risk[deployment.request_ref]?.state === 'available'}
						{@const decisions = risk[deployment.request_ref]?.value ?? []}
						<div class="mt-3 border-t border-white/5 pt-2">
							<h3 class="mb-1 text-[10px] font-semibold tracking-wider text-slate-600 uppercase">
								Risk decisions · {deployment.request_ref}
							</h3>
							<ul class="space-y-0.5 font-mono text-[10px]">
								{#each decisions as decision (decision.rule + decision.decision)}
									<li class="flex gap-2">
										<span class={decision.decision === 'allow' ? 'text-profit' : decision.decision === 'reject' ? 'text-loss' : 'text-warn'}>
											{decision.decision}
										</span>
										<span class="text-slate-400">{decision.rule}</span>
										{#if decision.reason}
											<span class="truncate text-slate-600">{decision.reason}</span>
										{/if}
									</li>
								{/each}
							</ul>
						</div>
					{/if}
				</li>
			{/each}
		</ul>
	{/if}

	<section class="rounded-xl border border-white/5 bg-surface-850/60">
		<h2 class="border-b border-white/5 px-4 py-2.5 text-sm font-semibold text-slate-200">
			Orders &amp; fills
		</h2>
		{#if orders.length}
			<table class="w-full border-collapse font-mono text-xs" data-testid="orders-table">
				<thead>
					<tr class="text-left text-[10px] tracking-wider text-slate-500 uppercase">
						<th class="px-4 py-2">Order</th>
						<th class="px-4 py-2">Symbol</th>
						<th class="px-4 py-2">Side</th>
						<th class="px-4 py-2 text-right">Qty</th>
						<th class="px-4 py-2 text-right">Price</th>
						<th class="px-4 py-2">Status</th>
						<th class="px-4 py-2">Mode</th>
						<th class="px-4 py-2 text-right">When</th>
					</tr>
				</thead>
				<tbody>
					{#each orders as order (order.id)}
						<tr class="border-t border-white/5">
							<td class="px-4 py-1.5 text-slate-500">{order.id}</td>
							<td class="px-4 py-1.5 text-slate-200">{order.symbol}</td>
							<td class="px-4 py-1.5 {order.side === 'buy' ? 'text-profit' : 'text-loss'}">{order.side}</td>
							<td class="px-4 py-1.5 text-right text-slate-300">{order.quantity}</td>
							<td class="px-4 py-1.5 text-right text-slate-300">{order.price}</td>
							<td class="px-4 py-1.5 text-slate-400">{order.status}</td>
							<td class="px-4 py-1.5">
								<span class="rounded-full border px-2 py-0.5 text-[10px] uppercase {MODE_BADGE[order.mode]}">
									{order.mode}
								</span>
							</td>
							<td class="px-4 py-1.5 text-right text-slate-600">{relativeTime(order.created_at)}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		{:else}
			<p class="px-4 py-6 text-center text-xs text-slate-600">No orders recorded.</p>
		{/if}
	</section>

	<section class="space-y-2">
		<h2 class="text-sm font-semibold text-slate-200">Portfolio (harness-published)</h2>
		<StatusSurfaceView surface={portfolio} label="Portfolio">
			{#snippet children(value)}
				{@const summary = value as PortfolioSummary}
				<div class="rounded-xl border border-white/5 bg-surface-850/60 p-4 font-mono text-xs" data-testid="portfolio-summary">
					<div class="mb-2 flex gap-6">
						<span class="text-slate-500">cash <span class="text-slate-200">{summary.cash}</span></span>
						<span class="text-slate-500">equity <span class="text-accent">{summary.equity}</span></span>
					</div>
					<ul class="space-y-2">
						{#each summary.books as book (book.name)}
							<li class="rounded-lg border border-white/5 bg-surface-900/60 px-3 py-2">
								<div class="flex gap-4">
									<span class="font-semibold text-slate-200">{book.name}</span>
									<span class="text-slate-500">cash {book.cash}</span>
									<span class="text-slate-500">equity {book.equity}</span>
								</div>
								{#if book.positions.length}
									<p class="mt-1 text-slate-600">
										{#each book.positions as position (position.symbol)}
											{position.symbol} {position.quantity}{position.average_price ? ` @ ${position.average_price}` : ''}
										{/each}
									</p>
								{/if}
							</li>
						{/each}
					</ul>
				</div>
			{/snippet}
		</StatusSurfaceView>
	</section>
</div>
