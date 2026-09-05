<script>
	let {
		title = '',
		equity = [],
		trades = [],
		instruments = [],
		layout = { above: [], below: [] },
		height = 560
	} = $props();

	const PAD = { top: 10, right: 70, bottom: 22, left: 70 };
	const EQUITY_FRACTION = 0.34;
	const MIN_SPAN = 5 * 24 * 60 * 60 * 1000;
	const FRAME = '#30363d';
	const GRID = 'rgba(48, 54, 61, 0.45)';

	let wrap;
	let canvas;
	let size = $state({ w: 960, h: 560 });
	let view = $state(null);
	let hover = $state(null);
	let dragging = false;
	let dragX = 0;
	let dragView = null;

	const bySymbol = $derived(Object.fromEntries(instruments.map((i) => [i.symbol, i])));
	const upper = $derived(layout.above.map((s) => bySymbol[s]).filter(Boolean));
	const lower = $derived(layout.below.map((s) => bySymbol[s]).filter(Boolean));

	const extent = $derived.by(() => {
		let t0 = Infinity;
		let t1 = -Infinity;
		for (const p of equity) {
			if (p.t < t0) t0 = p.t;
			if (p.t > t1) t1 = p.t;
		}
		for (const inst of instruments) {
			for (const p of inst.series) {
				if (p.t < t0) t0 = p.t;
				if (p.t > t1) t1 = p.t;
			}
		}
		return Number.isFinite(t0) && t1 > t0 ? { t0, t1 } : null;
	});

	$effect(() => {
		if (extent && !view) view = { t0: extent.t0, t1: extent.t1 };
	});

	$effect(() => {
		const el = wrap;
		const cv = canvas;
		if (!el || !cv) return;
		const ro = new ResizeObserver((entries) => {
			const r = entries[0].contentRect;
			size = { w: Math.max(320, r.width), h: Math.max(320, r.height) };
		});
		ro.observe(el);
		const onWheel = (e) => {
			if (!view || !extent) return;
			e.preventDefault();
			const rect = cv.getBoundingClientRect();
			const x = e.clientX - rect.left;
			const span = view.t1 - view.t0;
			const frac = (x - PAD.left) / Math.max(1, rect.width - PAD.left - PAD.right);
			const anchor = view.t0 + frac * span;
			let next = Math.min(Math.max(span * Math.exp(e.deltaY * 0.0015), MIN_SPAN), extent.t1 - extent.t0);
			let t0 = anchor - frac * next;
			t0 = Math.max(extent.t0, Math.min(t0, extent.t1 - next));
			view = { t0, t1: t0 + next };
		};
		cv.addEventListener('wheel', onWheel, { passive: false });
		return () => {
			ro.disconnect();
			cv.removeEventListener('wheel', onWheel);
		};
	});

	function xFor(t, width) {
		return PAD.left + ((t - view.t0) / (view.t1 - view.t0)) * (width - PAD.left - PAD.right);
	}

	function tFor(x, width) {
		const span = view.t1 - view.t0;
		return view.t0 + ((x - PAD.left) / Math.max(1, width - PAD.left - PAD.right)) * span;
	}

	function panelList(height) {
		const inner = height - PAD.top - PAD.bottom;
		const sideCount = Math.max(upper.length, lower.length);
		const sideH = sideCount ? (inner * (1 - EQUITY_FRACTION)) / sideCount : 0;
		const midH = inner - sideH * (upper.length + lower.length);
		const list = [];
		let y = PAD.top;
		for (const inst of upper) {
			list.push({ kind: 'price', inst, y, h: sideH });
			y += sideH;
		}
		list.push({ kind: 'equity', y, h: Math.max(60, midH) });
		y += Math.max(60, midH);
		for (const inst of lower) {
			list.push({ kind: 'price', inst, y, h: sideH });
			y += sideH;
		}
		return list;
	}

	function rangeFor(series, t0, t1, key) {
		let lo = Infinity;
		let hi = -Infinity;
		for (const p of series) {
			if (p.t < t0 || p.t > t1) continue;
			const v = p[key];
			if (v < lo) lo = v;
			if (v > hi) hi = v;
		}
		if (!Number.isFinite(lo) || lo === hi) return null;
		const pad = (hi - lo) * 0.08;
		return { lo: lo - pad, hi: hi + pad };
	}

	function yFor(panel, r, v) {
		return panel.y + panel.h - ((v - r.lo) / (r.hi - r.lo)) * (panel.h - 26) - 13;
	}

	function lastValueAt(series, t, key) {
		let prev = null;
		for (const p of series) {
			if (p.t > t) break;
			prev = p;
		}
		return prev ? prev[key] : undefined;
	}

	$effect(() => {
		if (!canvas || !view || !extent || !size) return;
		const dpr = Math.max(1, window.devicePixelRatio || 1);
		const { w, h } = size;
		canvas.width = Math.round(w * dpr);
		canvas.height = Math.round(h * dpr);
		const ctx = canvas.getContext('2d');
		ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
		ctx.clearRect(0, 0, w, h);
		ctx.font = '11px system-ui, sans-serif';
		const ps = panelList(h);
		for (let i = 0; i < ps.length; i++) drawPanel(ctx, w, h, ps[i], i === ps.length - 1);
		if (hover) drawCrosshair(ctx, w, h, ps);
	});

	function drawPanel(ctx, w, h, panel, isBottom) {
		const isEquity = panel.kind === 'equity';
		const series = isEquity ? equity : panel.inst.series;
		const key = isEquity ? 'v' : 'price';
		const r = rangeFor(series, view.t0, view.t1, key);
		if (!r) return;
		ctx.strokeStyle = FRAME;
		ctx.lineWidth = 1;
		ctx.strokeRect(PAD.left - 0.5, panel.y - 0.5, w - PAD.left - PAD.right + 1, panel.h + 1);
		ctx.strokeStyle = GRID;
		ctx.fillStyle = '#8b949e';
		for (let i = 0; i <= 3; i++) {
			const v = r.lo + ((r.hi - r.lo) * i) / 3;
			const y = yFor(panel, r, v);
			ctx.beginPath();
			ctx.moveTo(PAD.left, y);
			ctx.lineTo(w - PAD.right, y);
			ctx.stroke();
			ctx.fillText(fmtValue(v), 6, y + 4);
			ctx.fillText(fmtValue(v), w - PAD.right + 6, y + 4);
		}
		ctx.strokeStyle = isEquity ? '#58a6ff' : '#8b949e';
		ctx.lineWidth = 1.4;
		ctx.beginPath();
		let started = false;
		for (const p of series) {
			if (p.t < view.t0 || p.t > view.t1) continue;
			const x = xFor(p.t, w);
			const y = yFor(panel, r, p[key]);
			if (!started) {
				ctx.moveTo(x, y);
				started = true;
			} else ctx.lineTo(x, y);
		}
		ctx.stroke();
		ctx.lineWidth = 1;
		if (isEquity) drawEquityTrades(ctx, w, panel, r);
		else drawPriceTrades(ctx, w, panel, r);
		const label = isEquity
			? `${title || 'Equity'}   ${fmtValue(lastValueAt(series, view.t1, key))}`
			: `${panel.inst.symbol}   ${fmtValue(lastValueAt(series, view.t1, key))}`;
		ctx.fillStyle = '#e6edf3';
		ctx.fillText(label, PAD.left + 8, panel.y + 15);
		drawTimeTicks(ctx, w, panel, isBottom);
	}

	function fmtValue(v) {
		if (v === undefined || v === null || !Number.isFinite(v)) return '';
		return v >= 10000 ? v.toLocaleString('en-US', { maximumFractionDigits: 0 }) : v.toFixed(2);
	}

	function drawTimeTicks(ctx, w, panel, isBottom) {
		if (!isBottom) return;
		const span = view.t1 - view.t0;
		ctx.fillStyle = '#8b949e';
		for (let i = 0; i <= 6; i++) {
			const t = view.t0 + (span * i) / 6;
			const x = xFor(t, w);
			ctx.fillText(shortDate(t), Math.min(x + 3, w - PAD.right - 40), panel.y + panel.h - 5);
		}
	}

	function shortDate(t) {
		return new Date(t).toLocaleDateString('en-US', { month: 'short', year: '2-digit' });
	}

	function drawEquityTrades(ctx, w, panel, r) {
		for (const tr of trades) {
			if (tr.exitTime < view.t0 || tr.exitTime > view.t1) continue;
			const v = lastValueAt(equity, tr.exitTime, 'v');
			if (v === undefined) continue;
			const x = xFor(tr.exitTime, w);
			const y = yFor(panel, r, v);
			ctx.beginPath();
			ctx.arc(x, y, 3.2, 0, Math.PI * 2);
			ctx.fillStyle = tr.pnl >= 0 ? '#3fb950' : '#f85149';
			ctx.fill();
		}
	}

	function drawPriceTrades(ctx, w, panel, r) {
		for (const tr of trades) {
			if (tr.symbol !== panel.inst.symbol) continue;
			if (tr.exitTime < view.t0 || tr.entryTime > view.t1) continue;
			tick(ctx, xFor(tr.entryTime, w), yFor(panel, r, tr.entryPrice), tr.side === 'buy', false);
			tick(ctx, xFor(tr.exitTime, w), yFor(panel, r, tr.exitPrice), tr.side !== 'buy', true);
		}
	}

	function tick(ctx, x, y, up, hollow) {
		ctx.beginPath();
		if (up) {
			ctx.moveTo(x, y - 5);
			ctx.lineTo(x - 4, y + 4);
			ctx.lineTo(x + 4, y + 4);
		} else {
			ctx.moveTo(x, y + 5);
			ctx.lineTo(x - 4, y - 4);
			ctx.lineTo(x + 4, y + 4);
		}
		ctx.closePath();
		if (hollow) {
			ctx.strokeStyle = up ? '#3fb950' : '#f85149';
			ctx.lineWidth = 1.4;
			ctx.stroke();
			ctx.lineWidth = 1;
		} else {
			ctx.fillStyle = up ? '#3fb950' : '#f85149';
			ctx.fill();
		}
	}

	function drawCrosshair(ctx, w, h, ps) {
		const x = hover.x;
		ctx.strokeStyle = 'rgba(230, 237, 243, 0.35)';
		ctx.beginPath();
		ctx.moveTo(x, PAD.top);
		ctx.lineTo(x, h - PAD.bottom);
		ctx.stroke();
		for (const panel of ps) {
			const isEquity = panel.kind === 'equity';
			const series = isEquity ? equity : panel.inst.series;
			const key = isEquity ? 'v' : 'price';
			const r = rangeFor(series, view.t0, view.t1, key);
			const v = lastValueAt(series, hover.t, key);
			if (r === null || v === undefined) continue;
			const y = yFor(panel, r, v);
			ctx.fillStyle = isEquity ? '#58a6ff' : '#e6edf3';
			ctx.beginPath();
			ctx.arc(x, y, 3, 0, Math.PI * 2);
			ctx.fill();
			ctx.fillStyle = '#161b22';
			ctx.fillRect(w - PAD.right + 3, y - 7, PAD.right - 6, 14);
			ctx.fillStyle = isEquity ? '#58a6ff' : '#e6edf3';
			ctx.fillText(fmtValue(v), w - PAD.right + 7, y + 4);
		}
		if (hover.trade) {
			const tr = hover.trade;
			const lines = [
				`${tr.id}  ${tr.symbol}  ${tr.side.toUpperCase()} ${tr.qty}`,
				`entry ${tr.entryPrice.toFixed(2)}   exit ${tr.exitPrice.toFixed(2)}`,
				`pnl ${tr.pnl >= 0 ? '+' : ''}${tr.pnl.toFixed(0)}`
			];
			const bw = Math.max(...lines.map((l) => ctx.measureText(l).width)) + 16;
			const bx = Math.min(x + 12, w - PAD.right - bw);
			const by = PAD.top + 4;
			ctx.fillStyle = 'rgba(22, 27, 34, 0.95)';
			ctx.fillRect(bx, by, bw, 14 * lines.length + 8);
			ctx.strokeStyle = FRAME;
			ctx.strokeRect(bx + 0.5, by + 0.5, bw - 1, 14 * lines.length + 7);
			ctx.fillStyle = '#e6edf3';
			lines.forEach((l, i) => ctx.fillText(l, bx + 8, by + 18 + i * 14));
		}
	}

	function handleMove(e) {
		const rect = canvas.getBoundingClientRect();
		const x = e.clientX - rect.left;
		if (dragging) {
			const span = dragView.t1 - dragView.t0;
			const dt = ((x - dragX) / Math.max(1, rect.width - PAD.left - PAD.right)) * span;
			let t0 = dragView.t0 - dt;
			t0 = Math.max(extent.t0, Math.min(t0, extent.t1 - span));
			view = { t0, t1: t0 + span };
			hover = { x, t: tFor(x, rect.width), trade: null };
			return;
		}
		const t = tFor(x, rect.width);
		let trade = null;
		for (const tr of trades) {
			const ex = xFor(tr.exitTime, rect.width);
			if (Math.abs(ex - x) < 5) trade = tr;
		}
		hover = { x, t, trade };
	}

	function handleDown(e) {
		const rect = canvas.getBoundingClientRect();
		dragging = true;
		dragX = e.clientX - rect.left;
		dragView = { ...view };
		canvas.setPointerCapture(e.pointerId);
	}

	function handleUp() {
		dragging = false;
		dragView = null;
	}

	function handleLeave() {
		hover = null;
		dragging = false;
		dragView = null;
	}

	function reset() {
		if (extent) view = { t0: extent.t0, t1: extent.t1 };
	}
</script>

<figure bind:this={wrap} class="wrap">
	{#if title}
		<figcaption>{title}</figcaption>
	{/if}
	<div class="holder" style="height: {height}px">
		<canvas
			bind:this={canvas}
			onpointerdown={handleDown}
			onpointermove={handleMove}
			onpointerup={handleUp}
			onpointerleave={handleLeave}
			ondblclick={reset}
		></canvas>
		{#if view}
			<div class="hud">
				{shortDate(view.t0)} &rarr; {shortDate(view.t1)}
				<span class="hint">wheel zoom &middot; drag pan &middot; double-click reset</span>
			</div>
		{/if}
	</div>
</figure>

<style>
	.wrap {
		margin: 0;
		background: var(--panel);
		border: 1px solid var(--border);
		border-radius: 8px;
		overflow: hidden;
	}
	figcaption {
		padding: 8px 12px;
		font-size: 13px;
		color: var(--muted);
		border-bottom: 1px solid var(--border);
	}
	.holder {
		position: relative;
		width: 100%;
	}
	canvas {
		display: block;
		width: 100%;
		height: 100%;
		cursor: crosshair;
	}
	.hud {
		position: absolute;
		top: 6px;
		right: 10px;
		font-size: 12px;
		color: var(--text);
		background: rgba(22, 27, 34, 0.85);
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 3px 8px;
		pointer-events: none;
	}
	.hint {
		color: var(--muted);
		margin-left: 10px;
	}
</style>
