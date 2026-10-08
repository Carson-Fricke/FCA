<script>
	// @ts-nocheck — legacy JS component retained and restyled, not rewritten
	import TimeSeriesChart from '$lib/components/charts/TimeSeriesChart.svelte';
	import HistogramChart from '$lib/components/charts/HistogramChart.svelte';
	import StatsTable from '$lib/components/backtest-view/StatsTable.svelte';

	let {
		report,
		camera = $bindable(null),
		height = 300,
		previewHeight = 170,
		defaultExpanded = false,
		showFillsTable = true
	} = $props();

	const UP = '#34d399';
	const DOWN = '#f87171';

	// svelte-ignore state_referenced_locally
	let expanded = $state(defaultExpanded);
	let activeTab = $state('pnl');

	function toggle() {
		expanded = !expanded;
	}

	function handleKey(e) {
		if (e.key === 'Enter' || e.key === ' ') {
			e.preventDefault();
			toggle();
		}
	}

	const equity = $derived(report?.equity ?? []);
	const trades = $derived(report?.trades ?? []);
	const instruments = $derived(report?.instruments ?? []);
	const stats = $derived(report?.stats ?? null);
	const panels = $derived(report?.panels ?? []);

	function day(t) {
		return new Date(t).toLocaleDateString('en-US', { month: 'short', day: '2-digit', year: '2-digit' });
	}

	function tradeColor(pnl) {
		return pnl >= 0 ? UP : DOWN;
	}

	function valueAt(series, t, key = 'v') {
		let prev = null;
		for (const p of series) {
			if (p.t > t) break;
			prev = p;
		}
		return prev ? prev[key] : undefined;
	}

	function drawdownSeriesPercent(points) {
		let peak = -Infinity;
		return points.map((p) => {
			peak = Math.max(peak, p.v);
			return { t: p.t, v: peak > 0 ? ((p.v - peak) / peak) * 100 : 0 };
		});
	}

	const summary = $derived.by(() => {
		const wins = trades.filter((t) => t.pnl >= 0).length;
		const realized = trades.reduce((a, t) => a + t.pnl, 0);
		const first = equity[0]?.v ?? 0;
		const last = equity.at(-1)?.v ?? 0;
		return {
			trades: trades.length,
			wins,
			losses: trades.length - wins,
			realized,
			returnPct: first ? ((last - first) / first) * 100 : 0
		};
	});

	const fillMarkers = $derived(
		trades.flatMap((tr) => {
			const color = tradeColor(tr.pnl);
			const out = [];
			const ev = valueAt(equity, tr.entryTime, 'v');
			const xv = valueAt(equity, tr.exitTime, 'v');
			if (ev !== undefined) {
				out.push({
					t: tr.entryTime,
					v: ev,
					dir: tr.side === 'buy' ? 'up' : 'down',
					color,
					hollow: false,
					label: [`fill ${tr.id} ${tr.symbol} ${tr.side.toUpperCase()} ${tr.qty} @ ${tr.entryPrice.toFixed(2)}`]
				});
			}
			if (xv !== undefined) {
				out.push({
					t: tr.exitTime,
					v: xv,
					dir: tr.side === 'buy' ? 'down' : 'up',
					color,
					hollow: true,
					label: [
						`close ${tr.id} @ ${tr.exitPrice.toFixed(2)}`,
						`pnl ${tr.pnl >= 0 ? '+' : ''}${tr.pnl.toFixed(0)}`
					]
				});
			}
			return out;
		})
	);

	const equitySeries = $derived([{ name: 'Equity', data: equity }]);

	const drawdown = $derived.by(() => {
		const dd = drawdownSeriesPercent(equity);
		const max = dd.length ? Math.min(...dd.map((p) => p.v)) : 0;
		return { series: [{ name: 'Drawdown', data: dd, color: DOWN }], max };
	});

	const securityCharts = $derived(
		instruments.map((inst) => ({
			key: inst.symbol,
			chart: {
				title:
					inst.underlying && inst.underlying !== inst.symbol
						? `${inst.symbol} (underlying ${inst.underlying})`
						: inst.symbol,
				data: inst.series,
				render: 'candles',
				markers: trades
					.filter((tr) => tr.symbol === inst.symbol)
					.flatMap((tr) => {
						const color = tradeColor(tr.pnl);
						return [
							{
								t: tr.entryTime,
								v: tr.entryPrice,
								dir: tr.side === 'buy' ? 'up' : 'down',
								color,
								hollow: false,
								label: [
									`fill ${tr.id} ${tr.side.toUpperCase()} ${tr.qty} @ ${tr.entryPrice.toFixed(2)}`,
									day(tr.entryTime)
								]
							},
							{
								t: tr.exitTime,
								v: tr.exitPrice,
								dir: tr.side === 'buy' ? 'down' : 'up',
								color,
								hollow: true,
								label: [
									`close ${tr.id} @ ${tr.exitPrice.toFixed(2)}`,
									`pnl ${tr.pnl >= 0 ? '+' : ''}${tr.pnl.toFixed(0)}`,
									day(tr.exitTime)
								]
							}
						];
					}),
				height
			}
		}))
	);

	const panelTabs = $derived.by(() => {
		const groups = new Map();
		for (const panel of panels) {
			const group = panel.group ?? 'Panels';
			if (!groups.has(group)) groups.set(group, []);
			groups.get(group).push(panel);
		}
		return [...groups.entries()].map(([group, list]) => ({
			id: `panel:${group}`,
			label: group,
			charts: list.map((panel) => ({
				key: panel.id,
				chart: {
					title: panel.label ?? group,
					series: panel.series,
					data: panel.data,
					render: panel.render ?? 'line',
					markers: panel.markers ?? [],
					height
				}
			}))
		}));
	});

	const riskTiles = $derived.by(() => {
		if (!stats) return [];
		const out = [];
		if (stats.sharpePerSample != null) out.push({ label: 'Sharpe (per sample)', text: stats.sharpePerSample.toFixed(2), cls: '' });
		if (stats.sortino != null) out.push({ label: 'Sortino', text: stats.sortino.toFixed(2), cls: '' });
		if (stats.maxDrawdown != null)
			out.push({ label: 'Max DD', text: `${(stats.maxDrawdown * 100).toFixed(1)}%`, cls: 'neg' });
		if (stats.profitFactor != null)
			out.push({ label: 'Profit factor', text: stats.profitFactor.toFixed(2), cls: '' });
		if (stats.hitRate != null)
			out.push({ label: 'Hit rate', text: `${(stats.hitRate * 100).toFixed(1)}%`, cls: '' });
		return out;
	});

	const tabs = $derived.by(() => {
		const list = [
			{
				id: 'pnl',
				label: 'PnL & orders',
				charts: [
					{
						key: 'equity',
						chart: {
							title: 'Equity / cumulative PnL',
							series: equitySeries,
							render: 'line',
							markers: fillMarkers,
							height
						}
					}
				],
				table: true
			}
		];
		if (securityCharts.length) {
			list.push({ id: 'securities', label: 'Securities', charts: securityCharts });
		}
		list.push({ id: 'risk', label: 'Risk analysis' });
		list.push(...panelTabs);
		if (stats) list.push({ id: 'stats', label: 'Statistics' });
		return list;
	});

	const active = $derived(tabs.find((tab) => tab.id === activeTab) ?? tabs[0]);

	const tiles = $derived.by(() => {
		const out = [];
		const pnl = stats?.totalPnl ?? null;
		out.push({
			label: 'PnL',
			text:
				pnl == null
					? '—'
					: `${pnl >= 0 ? '+' : ''}${pnl.toLocaleString('en-US', { maximumFractionDigits: 0 })}`,
			cls: pnl == null ? '' : pnl >= 0 ? 'pos' : 'neg'
		});
		const ret = stats?.totalReturn ?? null;
		out.push({
			label: 'Return',
			text: ret == null ? '—' : `${ret * 100 >= 0 ? '+' : ''}${(ret * 100).toFixed(1)}%`,
			cls: ret == null ? '' : ret >= 0 ? 'pos' : 'neg'
		});
		const count = stats?.tradeCount ?? null;
		out.push({
			label: 'Trades',
			text: count == null ? '—' : `${count} (${stats?.winCount ?? '—'}W/${stats?.lossCount ?? '—'}L)`,
			cls: ''
		});
		if (stats?.sharpePerSample != null) {
			out.push({ label: 'Sharpe (per sample)', text: stats.sharpePerSample.toFixed(2), cls: '' });
		}
		if (stats?.maxDrawdown != null) {
			out.push({ label: 'Max DD', text: `${(stats.maxDrawdown * 100).toFixed(1)}%`, cls: '' });
		}
		return out;
	});
</script>

<section class="card">
	<header class="head" role="button" tabindex="0" aria-expanded={expanded} onclick={toggle} onkeydown={handleKey}>
		<div class="toprow">
			<span class="title">{report?.title ?? 'Backtest report'}</span>
			<span class="chev" class:open={expanded} aria-hidden="true">&#9662;</span>
		</div>
		<div class="strip">
			{#each tiles as tile (tile.label)}
				<div class="stat">
					<span class="k">{tile.label}</span>
					<span class="v" class:pos={tile.cls === 'pos'} class:neg={tile.cls === 'neg'}>{tile.text}</span>
				</div>
			{/each}
		</div>
	</header>

	{#if !expanded}
		<div class="preview">
			<TimeSeriesChart bind:camera series={equitySeries} render="line" markers={fillMarkers} height={previewHeight} title="PnL" />
		</div>
	{:else}
		<div class="tabs" role="tablist" aria-label="Report sections">
			{#each tabs as tab (tab.id)}
				<button
					type="button"
					role="tab"
					aria-selected={tab.id === active.id}
					class:active={tab.id === active.id}
					onclick={() => (activeTab = tab.id)}
				>
					{tab.label}
				</button>
			{/each}
		</div>
		<div class="body" role="tabpanel">
			{#if active.id === 'stats'}
				<StatsTable stats={stats} />
			{:else if active.id === 'risk'}
				{#if riskTiles.length}
					<div class="riskstrip">
						{#each riskTiles as tile (tile.label)}
							<div class="rtile">
								<span class="k">{tile.label}</span>
								<span class="v" class:pos={tile.cls === 'pos'} class:neg={tile.cls === 'neg'}>{tile.text}</span>
							</div>
						{/each}
					</div>
				{/if}
				<TimeSeriesChart bind:camera title="Equity / cumulative PnL" series={equitySeries} render="line" height={height} />
				<TimeSeriesChart bind:camera title={`Drawdown % \u00b7 max ${drawdown.max.toFixed(1)}%`} series={drawdown.series} render="line" height={Math.round(height * 0.7)} />
				<HistogramChart values={trades.map((tr) => tr.pnl)} height={200} label="Trade PnL distribution" />
			{:else}
				{#each active.charts as entry (entry.key)}
					<TimeSeriesChart
						bind:camera
						title={entry.chart.title}
						data={entry.chart.data}
						series={entry.chart.series}
						render={entry.chart.render}
						markers={entry.chart.markers}
						height={entry.chart.height}
					/>
				{/each}
				{#if active.table && showFillsTable}
					<div class="tablewrap">
						<table>
							<thead>
								<tr>
									<th>Order</th>
									<th>Symbol</th>
									<th>Side</th>
									<th>Qty</th>
									<th>Entry fill</th>
									<th>Exit fill</th>
									<th>PnL</th>
								</tr>
							</thead>
							<tbody>
								{#each trades as tr (tr.id)}
									<tr>
										<td>{tr.id}</td>
										<td>{tr.symbol}</td>
										<td class:pos={tr.side === 'buy'} class:neg={tr.side !== 'buy'}>{tr.side}</td>
										<td>{tr.qty}</td>
										<td>{tr.entryPrice.toFixed(2)} &middot; {day(tr.entryTime)}</td>
										<td>{tr.exitPrice.toFixed(2)} &middot; {day(tr.exitTime)}</td>
										<td class:pos={tr.pnl >= 0} class:neg={tr.pnl < 0}>
											{tr.pnl >= 0 ? '+' : ''}{tr.pnl.toFixed(0)}
										</td>
									</tr>
								{/each}
							</tbody>
						</table>
					</div>
				{/if}
			{/if}
		</div>
	{/if}
</section>

<style>
	.card {
		display: flex;
		flex-direction: column;
		gap: 10px;
		min-width: 0;
		background: var(--panel);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		padding: 12px;
		box-shadow: var(--shadow-card);
	}
	.head {
		display: flex;
		flex-direction: column;
		gap: 8px;
		cursor: pointer;
		border-radius: 8px;
	}
	.toprow {
		display: flex;
		align-items: center;
		gap: 10px;
	}
	.title {
		font-size: 13px;
		font-weight: 600;
		color: var(--text);
	}
	.chev {
		margin-left: auto;
		color: var(--muted);
		transition: transform 0.12s ease;
	}
	.chev.open {
		transform: rotate(180deg);
	}
	.strip,
	.riskstrip {
		display: flex;
		gap: 8px;
		flex-wrap: wrap;
	}
	.stat,
	.rtile {
		display: flex;
		flex-direction: column;
		gap: 1px;
		background: var(--surface-well);
		border-radius: 10px;
		padding: 5px 10px;
		min-width: 84px;
	}
	.stat .k,
	.rtile .k {
		font-size: 10px;
		color: var(--muted);
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}
	.stat .v,
	.rtile .v {
		font-family: var(--font-mono);
		font-size: 13px;
		font-weight: 500;
		color: var(--text);
		font-variant-numeric: tabular-nums;
	}
	.pos {
		color: var(--green);
	}
	.neg {
		color: var(--red);
	}
	.tabs {
		display: flex;
		gap: 4px;
		flex-wrap: wrap;
		border-bottom: 1px solid var(--border);
	}
	.tabs button {
		background: transparent;
		border: none;
		border-bottom: 2px solid transparent;
		color: var(--muted);
		font-size: 13px;
		font-family: inherit;
		padding: 6px 12px;
		cursor: pointer;
		transition: color 0.12s ease;
	}
	.tabs button:hover {
		color: var(--text);
	}
	.tabs button.active {
		color: var(--accent);
		border-bottom-color: var(--accent);
		font-weight: 600;
	}
	.body {
		display: flex;
		flex-direction: column;
		gap: 10px;
		max-height: 620px;
		overflow: auto;
		padding-right: 2px;
	}
	.body > :global(*) {
		flex-shrink: 0;
	}
	.tablewrap {
		background: var(--surface-well);
		border-radius: 10px;
		max-height: 260px;
		overflow: auto;
	}
	table {
		width: 100%;
		border-collapse: collapse;
		font-size: 12px;
	}
	.tablewrap th,
	.tablewrap td {
		text-align: left;
		padding: 5px 10px;
		border-bottom: 1px solid var(--border);
		white-space: nowrap;
	}
	.tablewrap th {
		position: sticky;
		top: 0;
		background: var(--surface-well);
		color: var(--muted);
		font-weight: 600;
		text-transform: uppercase;
		font-size: 10px;
		letter-spacing: 0.05em;
	}
	.tablewrap td {
		color: var(--text);
		font-variant-numeric: tabular-nums;
	}
</style>
