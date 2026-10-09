function mulberry32(seed) {
	let a = seed >>> 0;
	return function () {
		a |= 0;
		a = (a + 0x6d2b79f5) | 0;
		let t = Math.imul(a ^ (a >>> 15), 1 | a);
		t = (t ^ (t >>> 7) | 0);
		return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
	};
}

const DAY = 24 * 60 * 60 * 1000;

function walk(rand, days, start, drift, vol) {
	const out = [];
	let close = start;
	let t = Date.UTC(2023, 0, 3);
	for (let i = 0; i < days; i++) {
		const dow = new Date(t).getUTCDay();
		if (dow !== 0 && dow !== 6) {
			const open = close;
			close *= Math.exp(drift + vol * (rand() * 2 - 1));
			const spread = close * vol * (0.4 + rand() * 0.8);
			const high = Math.max(open, close) + spread * rand();
			const low = Math.min(open, close) - spread * rand();
			out.push({ t, open, high, low, close });
		}
		t += DAY;
	}
	return out;
}

function realizedBefore(trades, t) {
	let sum = 0;
	for (const tr of trades) {
		if (tr.exitTime <= t) sum += tr.pnl;
	}
	return sum;
}

function buildTrades(rand, instruments, equity) {
	const trades = [];
	const count = 64;
	for (let i = 0; i < count; i++) {
		const inst = instruments[Math.floor(rand() * instruments.length)];
		const series = inst.series;
		const i0 = 20 + Math.floor((rand() * (series.length - 60)) | 0);
		const hold = 3 + Math.floor(rand() * 18);
		const i1 = Math.min(series.length - 1, i0 + hold);
		const entry = series[i0];
		const exit = series[i1];
		const side = rand() > 0.45 ? 'buy' : 'sell';
		const qty = 10 * (1 + Math.floor(rand() * 9));
		const pnl = (exit.close - entry.close) * qty * (side === 'buy' ? 1 : -1);
		trades.push({
			id: `T${String(i + 1).padStart(3, '0')}`,
			symbol: inst.symbol,
			side,
			qty,
			entryTime: entry.t,
			entryPrice: entry.close,
			exitTime: exit.t,
			exitPrice: exit.close,
			pnl
		});
	}
	trades.sort((a, b) => a.exitTime - b.exitTime);
	const drift = 26000 / equity.length;
	for (let i = 0; i < equity.length; i++) {
		equity[i].v = 100000 + realizedBefore(trades, equity[i].t) + drift * i;
	}
	return trades;
}

function drawdownStats(equity) {
	let peak = equity[0]?.v ?? 0;
	let maxDd = 0;
	let maxStart = 0;
	let maxTrough = 0;
	let startT = equity[0]?.t ?? 0;
	let troughT = equity[0]?.t ?? 0;
	let spanStart = equity[0]?.t ?? 0;
	let spanNs = 0;
	for (const p of equity) {
		if (p.v >= peak) {
			peak = p.v;
			startT = p.t;
		} else {
			const dd = (peak - p.v) / peak;
			if (dd > maxDd) {
				maxDd = dd;
				maxStart = startT;
				maxTrough = p.t;
			}
			if (p.t - Math.max(spanStart, startT) > spanNs) {
				spanStart = startT;
				spanNs = p.t - startT;
			}
		}
	}
	return { maxDd, maxStart, maxTrough, spanNs };
}

function annualizedRatios(equity, periodsPerYear) {
	const returns = [];
	for (let i = 1; i < equity.length; i++) {
		if (equity[i - 1].v > 0) returns.push(equity[i].v / equity[i - 1].v - 1);
	}
	const mean = returns.reduce((a, r) => a + r, 0) / returns.length;
	const variance = returns.reduce((a, r) => a + (r - mean) * (r - mean), 0) / returns.length;
	const sd = Math.sqrt(variance);
	const downside = returns.filter((r) => r < 0);
	const dsd = Math.sqrt(
		downside.reduce((a, r) => a + r * r, 0) / (downside.length || 1)
	);
	const sharpe = sd > 0 ? (mean / sd) * Math.sqrt(periodsPerYear) : null;
	const sortino = dsd > 0 ? (mean / dsd) * Math.sqrt(periodsPerYear) : null;
	return { sharpe, sortino };
}

export const fixture = (() => {
	const rand = mulberry32(20260905);
	const days = 780;
	const spy = { symbol: 'SPY', underlying: 'SPY', series: walk(rand, days, 380, 0.00042, 0.011) };
	const qqq = { symbol: 'QQQ', underlying: 'QQQ', series: walk(rand, days, 315, 0.00055, 0.014) };
	const equity = spy.series.map((p) => ({ t: p.t, v: 100000 }));
	const trades = buildTrades(rand, [spy, qqq], equity);

	const first = equity[0].v;
	const last = equity.at(-1).v;
	const years = (equity.at(-1).t - equity[0].t) / (365.25 * 24 * 60 * 60 * 1000);
	const dd = drawdownStats(equity);
	const ratios = annualizedRatios(equity, 252);
	const wins = trades.filter((t) => t.pnl >= 0);
	const losses = trades.filter((t) => t.pnl < 0);
	const gross_profit = wins.reduce((a, t) => a + t.pnl, 0);
	const gross_loss = losses.reduce((a, t) => a + t.pnl, 0);
	const fills = trades.length * 2;
	const firstClose = spy.series[0].close;
	const benchmark = equity.map((p, i) => ({
		t: p.t,
		v: 100000 * (spy.series[i].close / firstClose)
	}));

	return {
		title: 'Reference equity strategy (fixture)',
		equity,
		trades,
		instruments: [spy, qqq],
		stats: {
			total_return: last / first - 1,
			cagr: Math.pow(last / first, 1 / years) - 1,
			max_drawdown: dd.maxDd,
			max_drawdown_duration_ns: dd.spanNs * 1e6,
			max_drawdown_recovered: true,
			sharpe: ratios.sharpe,
			sortino: ratios.sortino,
			trade_count: trades.length,
			win_count: wins.length,
			loss_count: losses.length,
			hit_rate: wins.length / trades.length,
			net_pnl: last - first,
			gross_profit,
			gross_loss,
			profit_factor: gross_profit / Math.abs(gross_loss),
			average_net_win: gross_profit / wins.length,
			average_net_loss: gross_loss / losses.length,
			largest_net_win: Math.max(...wins.map((t) => t.pnl)),
			largest_net_loss: Math.min(...losses.map((t) => t.pnl)),
			fill_count: fills,
			fees_total: fills * 1.0,
			slippage_total: fills * 0.8,
			latency_mean_ns: 840_000,
			latency_max_ns: 3_100_000,
			orders_total: fills,
			orders_filled: fills,
			orders_rejected: 0,
			orders_cancelled: 0
		},
		panels: [
			{
				id: 'benchmark',
				group: 'Benchmark',
				label: 'Equity vs buy-and-hold SPY',
				render: 'line',
				series: [
					{ name: 'Strategy', data: equity },
					{ name: 'SPY B&H', data: benchmark, color: '#94a3b8' }
				]
			}
		]
	};
})();
