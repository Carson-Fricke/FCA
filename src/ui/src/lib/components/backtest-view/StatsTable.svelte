<script>
	let { stats = {} } = $props();

	const KNOWN = {
		totalReturn: { label: 'Total return', group: 'Returns', format: 'pct', sign: true },
		cagr: { label: 'CAGR', group: 'Returns', format: 'pct', sign: true },
		maxDrawdown: { label: 'Max drawdown', group: 'Returns', format: 'pct', sign: false },
		maxDrawdownDurationNs: { label: 'Max DD duration', group: 'Returns', format: 'duration', sign: false },
		maxDrawdownRecovered: { label: 'DD recovered', group: 'Returns', format: 'bool', sign: false },
		sharpe: { label: 'Sharpe ratio', group: 'Returns', format: 'ratio', sign: false },
		sortino: { label: 'Sortino ratio', group: 'Returns', format: 'ratio', sign: false },
		tradeCount: { label: 'Trades', group: 'Trades', format: 'int', sign: false },
		winCount: { label: 'Wins', group: 'Trades', format: 'int', sign: false },
		lossCount: { label: 'Losses', group: 'Trades', format: 'int', sign: false },
		hitRate: { label: 'Hit rate', group: 'Trades', format: 'pct', sign: false },
		totalPnl: { label: 'Total PnL', group: 'Trades', format: 'money', sign: true },
		grossProfit: { label: 'Gross profit', group: 'Trades', format: 'money', sign: true },
		grossLoss: { label: 'Gross loss', group: 'Trades', format: 'money', sign: true },
		profitFactor: { label: 'Profit factor', group: 'Trades', format: 'ratio', sign: false },
		averageWin: { label: 'Average win', group: 'Trades', format: 'money', sign: true },
		averageLoss: { label: 'Average loss', group: 'Trades', format: 'money', sign: true },
		largestWin: { label: 'Largest win', group: 'Trades', format: 'money', sign: true },
		largestLoss: { label: 'Largest loss', group: 'Trades', format: 'money', sign: true },
		fillCount: { label: 'Fills', group: 'Execution', format: 'int', sign: false },
		feesTotal: { label: 'Fees', group: 'Execution', format: 'money', sign: false },
		slippageTotal: { label: 'Slippage cost', group: 'Execution', format: 'money', sign: false },
		latencyMeanNs: { label: 'Mean fill latency', group: 'Execution', format: 'ms', sign: false },
		latencyMaxNs: { label: 'Max fill latency', group: 'Execution', format: 'ms', sign: false },
		ordersTotal: { label: 'Orders', group: 'Execution', format: 'int', sign: false },
		ordersFilled: { label: 'Orders filled', group: 'Execution', format: 'int', sign: false },
		ordersRejected: { label: 'Orders rejected', group: 'Execution', format: 'int', sign: false },
		ordersCancelled: { label: 'Orders cancelled', group: 'Execution', format: 'int', sign: false }
	};
	const GROUP_ORDER = ['Returns', 'Trades', 'Execution', 'Other'];

	function fmtValue(value, format) {
		if (value == null || !Number.isFinite(value)) return '—';
		switch (format) {
			case 'pct':
				return `${(value * 100).toFixed(1)}%`;
			case 'ratio':
				return value.toFixed(2);
			case 'money':
				return `${value >= 0 ? '' : '-'}${Math.abs(value).toLocaleString('en-US', { maximumFractionDigits: 0 })}`;
			case 'ms':
				return `${(value / 1e6).toFixed(2)} ms`;
			case 'duration':
				return duration(value);
			case 'bool':
				return value ? 'yes' : 'no';
			case 'int':
				return value.toLocaleString('en-US');
			default:
				return String(value);
		}
	}

	function duration(ns) {
		const s = ns / 1e9;
		if (s < 60) return `${s.toFixed(1)} s`;
		const m = s / 60;
		if (m < 60) return `${m.toFixed(1)} min`;
		const h = m / 60;
		if (h < 48) return `${h.toFixed(1)} h`;
		return `${(h / 24).toFixed(1)} d`;
	}

	function prettify(key) {
		return key
			.replace(/([a-z0-9])([A-Z])/g, '$1 $2')
			.replace(/_/g, ' ')
			.replace(/^./, (c) => c.toUpperCase());
	}

	function guessFormat(value) {
		if (typeof value === 'boolean') return 'bool';
		if (typeof value === 'number' && Number.isFinite(value)) return 'num';
		return 'text';
	}

	const groups = $derived.by(() => {
		const buckets = new Map();
		const unknown = [];
		for (const [key, value] of Object.entries(stats)) {
			const spec = KNOWN[key];
			if (spec) {
				if (!buckets.has(spec.group)) buckets.set(spec.group, []);
				buckets.get(spec.group).push({ label: spec.label, value, format: spec.format, sign: spec.sign });
			} else {
				unknown.push({ label: prettify(key), value, format: guessFormat(value), sign: false });
			}
		}
		if (unknown.length) buckets.set('Other', unknown);
		return GROUP_ORDER.filter((g) => buckets.has(g)).map((label) => ({ label, rows: buckets.get(label) }));
	});

	function signed(row) {
		return row.sign && Number.isFinite(row.value) ? (row.value >= 0 ? 'pos' : 'neg') : null;
	}
</script>

<div class="groups">
	{#each groups as group (group.label)}
		<section class="group">
			<h4>{group.label}</h4>
			<table>
				<tbody>
					{#each group.rows as row (row.label)}
						<tr>
							<th scope="row">{row.label}</th>
							<td class:pos={signed(row) === 'pos'} class:neg={signed(row) === 'neg'}>
								{fmtValue(row.value, row.format)}
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</section>
	{/each}
</div>

<style>
	.groups {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
		gap: 10px;
		align-items: start;
	}
	.group {
		background: var(--surface-well);
		border-radius: 10px;
		overflow: hidden;
	}
	h4 {
		display: flex;
		align-items: center;
		gap: 8px;
		margin: 0;
		padding: 7px 12px;
		font-size: 10px;
		font-weight: 600;
		color: var(--muted);
		text-transform: uppercase;
		letter-spacing: 0.06em;
	}
	h4::before {
		content: '';
		width: 14px;
		height: 2px;
		border-radius: 1px;
		background: var(--accent);
	}
	table {
		width: 100%;
		border-collapse: collapse;
		font-size: 12px;
	}
	th,
	td {
		text-align: left;
		padding: 5px 12px;
		border-bottom: 1px solid var(--border);
		white-space: nowrap;
	}
	tr:last-child th,
	tr:last-child td {
		border-bottom: none;
	}
	th {
		color: var(--faint);
		font-weight: 400;
	}
	td {
		color: var(--text);
		text-align: right;
		font-family: var(--font-mono);
		font-variant-numeric: tabular-nums;
	}
	.pos {
		color: var(--green);
	}
	.neg {
		color: var(--red);
	}
</style>
