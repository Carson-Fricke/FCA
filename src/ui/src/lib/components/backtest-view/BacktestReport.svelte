<script>
	import TimeSeriesChart from '$lib/components/charts/TimeSeriesChart.svelte';

	let {
		report,
		camera = $bindable(null),
		height = 300,
		defaultExpanded = ['pnl', 'fills', 'securities'],
		showFillsTable = true
	} = $props();

	const UP = '#3fb950';
	const DOWN = '#f85149';

	let expanded = $state(null);

	function isOpen(id) {
		return (expanded ?? defaultExpanded).includes(id);
	}

	function toggle(id) {
		const base = expanded ?? defaultExpanded;
		expanded = base.includes(id) ? base.filter((x) => x !== id) : [...base, id];
	}

	const equity = $derived(report?.equity ?? []);
	const trades = $derived(report?.trades ?? []);
	const instruments = $derived(report?.instruments ?? []);
	const stats = $derived(report?.stats ?? null);

	function fmtPct(v) {
		return v == null || !Number.isFinite(v) ? '—' : `${(v * 100).toFixed(1)}%`;
	}

	function fmtRatio(v) {
		return v == null || !Number.isFinite(v) ? '—' : v.toFixed(2);
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
			finalEquity: last,
			returnPct: first ? ((last - first) / first) * 100 : 0
		};
	});

	function valueAt(series, t, key = 'v') {
		let prev = null;
		for (const p of series) {
			if (p.t > t) break;
			prev = p;
		}
		return prev ? prev[key] : undefined;
	}

	function tradeColor(pnl) {
		return pnl >= 0 ? UP : DOWN;
	}

	function day(t) {
		return new Date(t).toLocaleDateString('en-US', { month: 'short', day: '2-digit', year: '2-digit' });
	}

	const pnlMarkers = $derived(
		trades.flatMap((tr) => {
			const ev = valueAt(equity, tr.exitTime, 'v');
			if (ev === undefined) return [];
			return [
				{
					t: tr.exitTime,
					v: ev,
					dir: tr.pnl >= 0 ? 'up' : 'down',
					color: tradeColor(tr.pnl),
					hollow: false,
					label: [
						`close ${tr.id} ${tr.symbol} ${tr.side.toUpperCase()} ${tr.qty}`,
						`entry ${tr.entryPrice.toFixed(2)} -> exit ${tr.exitPrice.toFixed(2)}`,
						`pnl ${tr.pnl >= 0 ? '+' : ''}${tr.pnl.toFixed(0)}`
					]
				}
			];
		})
	);

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

	const sections = $derived.by(() => {
		const securityCharts = instruments.map((inst) => ({
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
		}));
		return [
			{
				id: 'pnl',
				label: 'PnL curve',
				meta: `${summary.realized >= 0 ? '+' : ''}${summary.realized.toLocaleString('en-US', { maximumFractionDigits: 0 })} realized`,
				charts: [{ key: 'equity', chart: { title: 'Equity / cumulative PnL', series: [{ name: 'Equity', data: equity }], render: 'line', markers: pnlMarkers, height } }],
				table: false
			},
			{
				id: 'fills',
				label: 'Orders and fills',
				meta: `${summary.trades} orders`,
				charts: [{ key: 'fills', chart: { title: 'Executions', series: [{ name: 'Equity', data: equity }], render: 'line', markers: fillMarkers, height } }],
				table: true
			},
			{
				id: 'securities',
				label: 'Traded securities',
				meta: instruments.map((i) => i.symbol).join(' · '),
				charts: securityCharts,
				table: false
			}
		];
	});
</script>

<section class="report">
	<header class="summary">
		<div class="stat">
			<span class="k">Realized PnL</span>
			<span class="v" class:pos={summary.realized >= 0} class:neg={summary.realized < 0}>
				{summary.realized >= 0 ? '+' : ''}{summary.realized.toLocaleString('en-US', { maximumFractionDigits: 0 })}
			</span>
		</div>
		<div class="stat">
			<span class="k">Return</span>
			<span class="v" class:pos={summary.returnPct >= 0} class:neg={summary.returnPct < 0}>
				{summary.returnPct >= 0 ? '+' : ''}{summary.returnPct.toFixed(1)}%
			</span>
		</div>
		<div class="stat">
			<span class="k">Trades</span>
			<span class="v">{summary.trades} <span class="dim">({summary.wins}W/{summary.losses}L)</span></span>
		</div>
		<div class="stat">
			<span class="k">Final equity</span>
			<span class="v">{summary.finalEquity.toLocaleString('en-US', { maximumFractionDigits: 0 })}</span>
		</div>
		{#if stats}
			<div class="stat">
				<span class="k">Win rate</span>
				<span class="v">{fmtPct(stats.winRate)}</span>
			</div>
			<div class="stat">
				<span class="k">CAGR</span>
				<span class="v" class:pos={stats.cagr >= 0} class:neg={stats.cagr < 0}>{fmtPct(stats.cagr)}</span>
			</div>
			<div class="stat">
				<span class="k">Sharpe</span>
				<span class="v">{fmtRatio(stats.sharpe)}</span>
			</div>
			<div class="stat">
				<span class="k">Sortino</span>
				<span class="v">{fmtRatio(stats.sortino)}</span>
			</div>
			<div class="stat">
				<span class="k">Max drawdown</span>
				<span class="v">{fmtPct(stats.maxDrawdown)}</span>
			</div>
		{/if}
	</header>

	{#each sections as sec (sec.id)}
		<div class="acc" class:open={isOpen(sec.id)}>
			<button class="head" type="button" onclick={() => toggle(sec.id)} aria-expanded={isOpen(sec.id)}>
				<span class="chev" aria-hidden="true">&#9656;</span>
				<span class="label">{sec.label}</span>
				<span class="meta">{sec.meta}</span>
			</button>
			{#if isOpen(sec.id)}
				<div class="body">
					{#each sec.charts as entry (entry.key)}
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
					{#if sec.table && showFillsTable}
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
				</div>
			{/if}
		</div>
	{/each}
</section>

<style>
	.report {
		display: flex;
		flex-direction: column;
		gap: 10px;
		min-width: 0;
	}
	.summary {
		display: flex;
		gap: 10px;
		flex-wrap: wrap;
	}
	.stat {
		display: flex;
		flex-direction: column;
		gap: 2px;
		background: var(--panel);
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 8px 14px;
		min-width: 110px;
	}
	.stat .k {
		font-size: 11px;
		color: var(--muted);
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}
	.stat .v {
		font-size: 16px;
		color: var(--text);
	}
	.dim {
		color: var(--muted);
		font-size: 12px;
	}
	.pos {
		color: #3fb950;
	}
	.neg {
		color: #f85149;
	}
	.acc {
		background: var(--panel);
		border: 1px solid var(--border);
		border-radius: 8px;
		overflow: hidden;
	}
	.head {
		display: flex;
		align-items: center;
		gap: 10px;
		width: 100%;
		padding: 9px 12px;
		background: transparent;
		border: none;
		color: var(--text);
		font-size: 13px;
		cursor: pointer;
		text-align: left;
	}
	.head:hover {
		background: rgba(48, 54, 61, 0.4);
	}
	.chev {
		color: var(--muted);
		transition: transform 0.12s ease;
	}
	.acc.open .chev {
		transform: rotate(90deg);
	}
	.label {
		font-weight: 600;
	}
	.meta {
		margin-left: auto;
		color: var(--muted);
		font-size: 12px;
	}
	.body {
		display: flex;
		flex-direction: column;
		gap: 10px;
		padding: 0 10px 10px;
	}
	.tablewrap {
		border: 1px solid var(--border);
		border-radius: 8px;
		max-height: 260px;
		overflow: auto;
	}
	table {
		width: 100%;
		border-collapse: collapse;
		font-size: 12px;
	}
	th,
	td {
		text-align: left;
		padding: 5px 10px;
		border-bottom: 1px solid var(--border);
		white-space: nowrap;
	}
	th {
		position: sticky;
		top: 0;
		background: var(--panel);
		color: var(--muted);
		font-weight: 600;
	}
	td {
		color: var(--text);
	}
</style>
