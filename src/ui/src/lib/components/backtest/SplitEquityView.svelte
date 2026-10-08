<script lang="ts">
	import TimeSeriesChart from '$lib/components/charts/TimeSeriesChart.svelte';
	import MetricCard from '$lib/components/common/MetricCard.svelte';
	import { shortDate } from '$lib/format';
	import type { BacktestDetail, EquityPoint, SegmentMetrics } from '$lib/types';

	/**
	 * Backtest detail equity view. Segments in-sample / out-of-sample at the
	 * manifest's declared split boundary with per-segment metric cards, and
	 * renders an explicit "not declared" state when the manifest carries no
	 * split — the split is never fabricated.
	 */
	let { backtest }: { backtest: BacktestDetail } = $props();

	const boundaryTime = $derived(
		backtest.split ? Date.parse(`${backtest.split.boundary}T00:00:00Z`) : Number.NaN
	);

	const inSample = $derived.by(() => {
		if (!backtest.split) return [] as EquityPoint[];
		return backtest.equity.filter((p) => p.t <= boundaryTime);
	});

	const outOfSample = $derived.by(() => {
		if (!backtest.split) return [] as EquityPoint[];
		const previous = backtest.equity.find((p) => p.t <= boundaryTime);
		const rest = backtest.equity.filter((p) => p.t > boundaryTime);
		return previous ? [previous, ...rest] : rest;
	});

	const splitMarker = $derived.by(() => {
		if (!backtest.split) return [];
		let point: EquityPoint | null = null;
		for (const p of backtest.equity) {
			if (p.t > boundaryTime) break;
			point = p;
		}
		return point
			? [{ t: point.t, v: point.v, dir: 'down' as const, color: '#fbbf24', label: ['in-sample | out-of-sample split', backtest.split.boundary] }]
			: [];
	});

	const overall = $derived.by(() => {
		const stats = backtest.stats ?? {};
		const published = (key: string): number | null => {
			const value = stats[key];
			return typeof value === 'number' ? value : null;
		};
		const totalReturn = published('totalReturn');
		return {
			returnPct: totalReturn == null ? null : totalReturn * 100,
			sharpe: published('sharpe'),
			maxDrawdown: published('maxDrawdown'),
			trades: published('tradeCount')
		};
	});

	function segmentTiles(label: string, metrics: SegmentMetrics) {
		return [
			{ label: `${label} return`, value: metrics.returnPct, format: 'signedPct' as const },
			{ label: `${label} Sharpe`, value: metrics.sharpe ?? '—', format: 'ratio' as const },
			{ label: `${label} max DD`, value: metrics.maxDrawdown, format: 'pct' as const },
			{ label: `${label} trades`, value: metrics.trades, format: 'text' as const }
		];
	}
</script>

{#if backtest.split}
	<div data-testid="split-declared" data-boundary={backtest.split.boundary}>
		<div
			class="mb-3 flex items-center gap-2 rounded-xl border border-accent/30 bg-accent/5 px-4 py-2.5 text-xs"
		>
			<span class="h-1.5 w-1.5 rounded-full bg-accent" aria-hidden="true"></span>
			<span class="font-semibold text-accent">Train/test split declared</span>
			<span class="font-mono text-slate-500">
				in-sample → {shortDate(backtest.split.boundary)} | out-of-sample {shortDate(backtest.split.boundary)} →
			</span>
			<span class="ml-auto font-mono text-[10px] text-slate-500">
				{inSample.length} + {outOfSample.length} points · boundary from manifest
			</span>
		</div>

		<div class="mb-3 grid grid-cols-2 gap-3 lg:grid-cols-4">
			{#each [...segmentTiles('In-sample', backtest.split.inSample), ...segmentTiles('Out-of-sample', backtest.split.outOfSample)] as tile (tile.label)}
				<MetricCard label={tile.label} value={tile.value} format={tile.format} />
			{/each}
		</div>

		<div class="grid gap-3 lg:grid-cols-2">
			<div>
				<TimeSeriesChart
					title="In-sample equity → {backtest.split.boundary}"
					data={inSample}
					render="line"
					height={260}
				/>
			</div>
			<div>
				<TimeSeriesChart
					title="Out-of-sample equity ({backtest.split.boundary} →)"
					data={outOfSample}
					render="line"
					height={260}
				/>
			</div>
		</div>
	</div>
{:else}
	<div data-testid="split-not-declared">
		<div
			class="mb-3 rounded-xl border border-dashed border-surface-600/70 bg-surface-950 px-4 py-2.5 text-xs"
		>
			<span class="font-semibold text-slate-700">Train/test split not declared</span>
			<span class="ml-2 text-slate-500">
				This run's manifest declares no in-sample/out-of-sample boundary; the curve is shown
				unsegmented. No split is fabricated.
			</span>
		</div>
		<div class="mb-3 grid grid-cols-2 gap-3 lg:grid-cols-4">
			<MetricCard label="Return" value={overall.returnPct ?? '—'} format="signedPct" />
			<MetricCard label="Sharpe" value={overall.sharpe ?? '—'} format="ratio" />
			<MetricCard label="Max drawdown" value={overall.maxDrawdown ?? '—'} format="pct" />
			<MetricCard label="Trades" value={overall.trades ?? '—'} />
		</div>
	</div>
{/if}

<TimeSeriesChart
	title="Equity / cumulative P&L"
	data={backtest.equity}
	render="line"
	markers={splitMarker}
	height={280}
/>

{#if backtest.trades.length}
	<div class="mt-3 overflow-hidden rounded-xl border border-surface-700">
		<table class="w-full border-collapse font-mono text-xs">
			<thead>
				<tr class="bg-surface-900 text-left text-[10px] tracking-wider text-slate-500 uppercase">
					<th class="px-3 py-2">Order</th>
					<th class="px-3 py-2">Symbol</th>
					<th class="px-3 py-2">Side</th>
					<th class="px-3 py-2">Qty</th>
					<th class="px-3 py-2">Entry</th>
					<th class="px-3 py-2">Exit</th>
					<th class="px-3 py-2 text-right">P&L</th>
				</tr>
			</thead>
			<tbody>
				{#each backtest.trades as trade (trade.id)}
					<tr class="border-t border-surface-700">
						<td class="px-3 py-1.5 text-slate-500">{trade.id}</td>
						<td class="px-3 py-1.5 text-slate-700">{trade.symbol}</td>
						<td class="px-3 py-1.5 {trade.side === 'buy' ? 'text-profit' : 'text-loss'}">{trade.side}</td>
						<td class="px-3 py-1.5 text-slate-700">{trade.qty}</td>
						<td class="px-3 py-1.5 text-slate-500">{trade.entryPrice.toFixed(2)}</td>
						<td class="px-3 py-1.5 text-slate-500">{trade.exitPrice.toFixed(2)}</td>
						<td class="px-3 py-1.5 text-right {trade.pnl >= 0 ? 'text-profit' : 'text-loss'}">
							{trade.pnl >= 0 ? '+' : ''}{trade.pnl.toFixed(0)}
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
{/if}

<p class="mt-2 font-mono text-[10px] text-slate-500">
	job {backtest.job_id} · digest {backtest.manifest.run_digest?.slice(0, 16) ?? '—'}…
</p>
