<script lang="ts">
	import TimeSeriesChart from '$lib/components/charts/TimeSeriesChart.svelte';
	import InlineConfirm from '$lib/components/common/InlineConfirm.svelte';
	import type { WidgetDefinition } from '$lib/types';

	/** One dashboard widget card for the declared widget types. */
	let {
		widget,
		onPin,
		onRemove
	}: {
		widget: WidgetDefinition;
		onPin: () => void;
		onRemove: () => void;
	} = $props();

	const bars = $derived.by(() => {
		const items = (widget.data['items'] as { label: string; share: number }[] | undefined) ?? [];
		return items;
	});

	const points = $derived.by(() => {
		const pts = (widget.data['points'] as { t: number; v: number }[] | undefined) ?? [];
		return pts;
	});

	const maxAbs = $derived(Math.max(1, ...bars.map((b) => Math.abs(b.share))));
</script>

<div
	class="flex flex-col rounded-xl border border-surface-700 bg-surface-900"
	data-testid="widget-card"
	data-widget-type={widget.type}
>
	<div class="flex items-center gap-2 border-b border-surface-700 px-3 py-2">
		<h3 class="truncate text-xs font-semibold text-slate-200">{widget.title}</h3>
		<span class="ml-auto flex items-center gap-1">
			<button
				type="button"
				class="rounded px-1.5 py-0.5 text-[10px] {widget.pinned
					? 'text-accent'
					: 'text-slate-600 hover:text-slate-300'}"
				title={widget.pinned ? 'Unpin from sidebar' : 'Pin to sidebar'}
				onclick={onPin}
				data-testid="widget-pin"
			>
				{widget.pinned ? '★ pinned' : '☆ pin'}
			</button>
			<InlineConfirm confirmLabel="Remove" onConfirm={onRemove}>
				{#snippet children()}
					<span class="px-1 text-[10px] text-slate-600 hover:text-loss" title="Remove widget">✕</span>
				{/snippet}
			</InlineConfirm>
		</span>
	</div>

	<div class="min-h-40 flex-1 p-2">
		{#if widget.type === 'equity' && points.length}
			<TimeSeriesChart title="Equity" data={points} render="line" height={180} />
		{:else if widget.type === 'pnl' && points.length}
			<TimeSeriesChart
				title="Daily P&L"
				series={[{ name: 'P&L', data: points, color: '#34d399', render: 'line', fill: true }]}
				height={180}
			/>
		{:else if bars.length}
			<ul class="space-y-2 px-1 py-2">
				{#each bars as bar (bar.label)}
					{@const positive = widget.type !== 'winrate' || bar.share >= 0}
					<li>
						<div class="mb-1 flex items-baseline justify-between text-[11px]">
							<span class="text-slate-400">{bar.label}</span>
							<span class="font-mono {positive ? 'text-slate-200' : 'text-loss'}">
								{widget.type === 'allocation'
									? `${(bar.share * 100).toFixed(0)}%`
									: widget.type === 'winrate'
										? `${(bar.share * 100).toFixed(0)}%`
										: bar.share.toLocaleString('en-US')}
							</span>
						</div>
						<div class="h-1.5 overflow-hidden rounded-full bg-surface-900">
							<div
								class="h-full rounded-full {widget.type === 'winrate'
									? 'bg-accent/70'
									: widget.type === 'orders'
										? 'bg-surface-600'
										: bar.share >= 0
											? 'bg-profit/70'
											: 'bg-loss/70'}"
								style="width: {Math.max(4, (Math.abs(bar.share) / maxAbs) * 100)}%"
							></div>
						</div>
					</li>
				{/each}
			</ul>
		{:else}
			<p class="px-2 py-6 text-center text-xs text-slate-600">Widget has no declared data.</p>
		{/if}
	</div>
</div>
