function mulberry32(seed) {
	let a = seed >>> 0;
	return function () {
		a |= 0;
		a = (a + 0x6d2b79f5) | 0;
		let t = Math.imul(a ^ (a >>> 15), 1 | a);
		t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
		return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
	};
}

const DAY = 24 * 60 * 60 * 1000;

function walk(rand, days, start, drift, vol) {
	const out = [];
	let price = start;
	let t = Date.UTC(2023, 0, 3);
	for (let i = 0; i < days; i++) {
		if (new Date(t).getUTCDay() !== 0 && new Date(t).getUTCDay() !== 6) {
			price *= Math.exp(drift + vol * (rand() * 2 - 1));
			out.push({ t, price });
		}
		t += DAY;
	}
	return out;
}

function buildTrades(rand, instruments, equity) {
	const bySymbol = Object.fromEntries(instruments.map((i) => [i.symbol, i]));
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
		const dir = side === 'buy' ? 1 : -1;
		const pnl = (exit.price - entry.price) * qty * (side === 'buy' ? 1 : -1);
		trades.push({
			id: `T${String(i + 1).padStart(3, '0')}`,
			symbol: inst.symbol,
			side,
			qty,
			entryTime: entry.t,
			entryPrice: entry.price,
			exitTime: exit.t,
			exitPrice: exit.price,
			pnl
		});
	}
	trades.sort((a, b) => a.exitTime - b.exitTime);
	equity.sort((a, b) => a.t - b.t);
	let ti = 0;
	let realized = 0;
	for (const point of equity) {
		while (ti < trades.length && trades[ti].exitTime <= point.t) {
			realized += trades[ti].pnl;
			ti += 1;
		}
		point.v = 100000 + realized + (point.t / (instruments[0].series.at(-1).t || 1)) * 0;
	}
	const drift = 26000 / equity.length;
	for (let i = 0; i < equity.length; i++) {
		equity[i].v = 100000 + realizedBefore(trades, equity[i].t) + drift * i;
	}
	return trades;
}

function realizedBefore(trades, t) {
	let sum = 0;
	for (const tr of trades) {
		if (tr.exitTime <= t) sum += tr.pnl;
	}
	return sum;
}

export const fixture = (() => {
	const rand = mulberry32(20260905);
	const days = 780;
	const spy = { symbol: 'SPY', series: walk(rand, days, 380, 0.00042, 0.011) };
	const qqq = { symbol: 'QQQ', series: walk(rand, days, 315, 0.00055, 0.014) };
	const equity = spy.series.map((p) => ({ t: p.t, v: 100000 }));
	const trades = buildTrades(rand, [spy, qqq], equity);
	return {
		title: 'Reference equity strategy (fixture)',
		equity,
		trades,
		instruments: [spy, qqq],
		layout: { above: ['SPY'], below: ['QQQ'] }
	};
})();
