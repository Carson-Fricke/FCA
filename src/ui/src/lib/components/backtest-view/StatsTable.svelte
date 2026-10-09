<script>
	// @ts-nocheck — legacy JS component retained and restyled, not rewritten
	let { stats = {} } = $props();

	const KNOWN = {
		total_return: { label: 'Total return', group: 'Returns', format: 'pct', sign: true },
		cagr: { label: 'CAGR', group: 'Returns', format: 'pct', sign: true },
		max_drawdown: { label: 'Max drawdown', group: 'Returns', format: 'pct', sign: false },
		max_drawdown_duration_ns: { label: 'Max DD duration', group: 'Returns', format: 'duration', sign: false },
		max_drawdown_recovered: { label: 'DD recovered', group: 'Returns', format: 'bool', sign: false },
		sharpe_per_sample: { label: 'Sharpe (per sample)', group: 'Returns', format: 'ratio', sign: false },
		sortino: { label: 'Sortino ratio', group: 'Returns', format: 'ratio', sign: false },
		trade_count: { label: 'Trades', group: 'Trades', format: 'int', sign: false },
		win_count: { label: 'Wins', group: 'Trades', format: 'int', sign: false },
		loss_count: { label: 'Losses', group: 'Trades', format: 'int', sign: false },
		hit_rate: { label: 'Hit rate', group: 'Trades', format: 'pct', sign: false },
		net_pnl: { label: 'Net PnL', group: 'Trades', format: 'money', sign: true },
		gross_profit: { label: 'Gross profit', group: 'Trades', format: 'money', sign: true },
		gross_loss: { label: 'Gross loss', group: 'Trades', format: 'money', sign: true },
		gross_pnl: { label: 'Gross PnL', group: 'Trades', format: 'money', sign: true },
		closed_trade_net_pnl: { label: 'Closed-trade net PnL', group: 'Trades', format: 'money', sign: true },
		closed_trade_gross_pnl: { label: 'Closed-trade gross PnL', group: 'Trades', format: 'money', sign: true },
		fees: { label: 'Closed-trade fees', group: 'Trades', format: 'money', sign: false },
		open_fees: { label: 'Open-position fees', group: 'Execution', format: 'money', sign: false },
		net_profit: { label: 'Net profit', group: 'Trades', format: 'money', sign: true },
		net_loss: { label: 'Net loss', group: 'Trades', format: 'money', sign: false },
		profit_factor: { label: 'Profit factor', group: 'Trades', format: 'ratio', sign: false },
		average_net_win: { label: 'Average win', group: 'Trades', format: 'money', sign: true },
		average_net_loss: { label: 'Average loss', group: 'Trades', format: 'money', sign: true },
		largest_net_win: { label: 'Largest win', group: 'Trades', format: 'money', sign: true },
		largest_net_loss: { label: 'Largest loss', group: 'Trades', format: 'money', sign: true },
		fill_count: { label: 'Fills', group: 'Execution', format: 'int', sign: false },
		fees_total: { label: 'Fees', group: 'Execution', format: 'money', sign: false },
		slippage_total: { label: 'Slippage cost', group: 'Execution', format: 'money', sign: false },
		latency_mean_ns: { label: 'Mean fill latency', group: 'Execution', format: 'ms', sign: false },
		latency_max_ns: { label: 'Max fill latency', group: 'Execution', format: 'ms', sign: false },
		orders_total: { label: 'Orders', group: 'Execution', format: 'int', sign: false },
		orders_filled: { label: 'Orders filled', group: 'Execution', format: 'int', sign: false },
		orders_rejected: { label: 'Orders rejected', group: 'Execution', format: 'int', sign: false },
		orders_cancelled: { label: 'Orders cancelled', group: 'Execution', format: 'int', sign: false }
	};
	const GROUP_ORDER = ['Returns', 'Trades', 'Execution', 'Other'];

	function fmtValue(value, format) {
		if (value == null || (typeof value === 'number' && !Number.isFinite(value))) return '—';
		if (typeof value === 'string' && value.trim() !== '' && Number.isFinite(Number(value))) value = Number(value);
		if (typeof value !== 'number') return format === 'bool' ? (value ? 'yes' : 'no') : String(value);
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
			if (key.endsWith('_reason')) {
				const metric = key.slice(0, -7);
				if (value == null || metric in stats) continue;
			}
			const spec = KNOWN[key];
			const reason = value == null ? stats[`${key}_reason`] ?? (spec?.group === 'Trades' ? stats.trades_unavailable_reason : null) : null;
			if (spec) {
				if (!buckets.has(spec.group)) buckets.set(spec.group, []);
				buckets.get(spec.group).push({ label: spec.label, value, format: spec.format, sign: spec.sign, reason });
			} else {
				unknown.push({ label: prettify(key), value, format: guessFormat(value), sign: false, reason });
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
								{fmtValue(row.value, row.format)}{#if row.reason} <span class="text-xs">({row.reason})</span>{/if}
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
