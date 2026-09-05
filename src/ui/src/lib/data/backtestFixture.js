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

export const fixture = (() => {
	const rand = mulberry32(20260905);
	const days = 780;
	const spy = { symbol: 'SPY', underlying: 'SPY', series: walk(rand, days, 380, 0.00042, 0.011) };
	const qqq = { symbol: 'QQQ', underlying: 'QQQ', series: walk(rand, days, 315, 0.00055, 0.014) };
	const equity = spy.series.map((p) => ({ t: p.t, v: 100000 }));
	const trades = buildTrades(rand, [spy, qqq], equity);
	return {
		title: 'Reference equity strategy (fixture)',
		equity,
		trades,
		instruments: [spy, qqq],
		stats: { winRate: 0.641, cagr: 0.142, sharpe: 1.87, sortino: 2.31 }
	};
})();
