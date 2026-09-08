<script>
	let {
		title = '',
		data = [],
		series = null,
		render = 'candles',
		camera = $bindable(null),
		markers = [],
		height = 320,
		minSpan = 5 * 24 * 60 * 60 * 1000,
		axis = 'date'
	} = $props();

	const PAD = { top: 10, right: 70, bottom: 30, left: 70 };
	const XPAD = 12;
	const FRAME = '#243044';
	const GRID = 'rgba(148, 163, 184, 0.1)';
	const AXIS = '#64748b';
	const UP = '#34d399';
	const DOWN = '#f87171';
	const ACCENT = '#22d3ee';
	const HANDLE = 'rgba(34, 211, 238, 0.35)';
	const PALETTE = ['#22d3ee', '#f472b6', '#fbbf24', '#34d399', '#a78bfa'];

	let holder;
	let canvas;
	let size = $state(null);
	let hover = $state(null);
	let valueWindow = $state(null);
	let drag = null;

	const plots = $derived.by(() => {
		if (series && series.length) {
			return series.map((s, i) => ({
				name: s.name ?? '',
				data: s.data ?? [],
				color: s.color ?? PALETTE[i % PALETTE.length],
				render: s.render ?? render,
				fill: s.fill ?? false
			}));
		}
		return data.length ? [{ name: '', data, color: ACCENT, render, fill: false }] : [];
	});

	const extent = $derived.by(() => {
		let t0 = Infinity;
		let t1 = -Infinity;
		for (const plot of plots) {
			for (const p of plot.data) {
				if (p.t < t0) t0 = p.t;
				if (p.t > t1) t1 = p.t;
			}
		}
		return Number.isFinite(t0) && t1 > t0 ? { t0, t1 } : null;
	});

	const fixedRange = $derived(
		valueWindow && valueWindow.hi > valueWindow.lo
			? { lo: valueWindow.lo, hi: valueWindow.hi }
			: null
	);

	$effect(() => {
		if (extent && !camera) camera = { t0: extent.t0, t1: extent.t1 };
	});

	$effect(() => {
		const el = holder;
		const cv = canvas;
		if (!el || !cv) return;
		const ro = new ResizeObserver((entries) => {
			const r = entries[0].contentRect;
			size = { w: Math.max(320, r.width), h: Math.max(120, r.height) };
		});
		ro.observe(el);
		const onWheel = (e) => {
			if (!camera || !extent) return;
			e.preventDefault();
			const rect = cv.getBoundingClientRect();
			const x = e.clientX - rect.left;
			const y = e.clientY - rect.top;
			if (e.shiftKey) {
				const r = effectiveRange(rect.height);
				const v = valueAtY(y, rect.height, r);
				if (r === null || v === undefined) return;
				const k = Math.exp(e.deltaY * 0.0015);
				const lo = v - (v - r.lo) * k;
				const hi = v + (r.hi - v) * k;
				if (hi - lo < 1e-9) return;
				valueWindow = { lo, hi };
				return;
			}
			const frac = (x - PAD.left) / Math.max(1, rect.width - PAD.left - PAD.right);
			const span = camera.t1 - camera.t0;
			const anchor = camera.t0 + frac * span;
			const next = Math.min(
				Math.max(span * Math.exp(e.deltaY * 0.0015), minSpan),
				extent.t1 - extent.t0
			);
			let t0 = anchor - frac * next;
			t0 = Math.max(extent.t0, Math.min(t0, extent.t1 - next));
			camera = { ...camera, t0, t1: t0 + next };
		};
		cv.addEventListener('wheel', onWheel, { passive: false });
		return () => {
			ro.disconnect();
			cv.removeEventListener('wheel', onWheel);
		};
	});

	function isBars(plot) {
		return plot.data.length > 0 && 'close' in plot.data[0];
	}

	function plotValue(plot, p) {
		return isBars(plot) ? p.close : (p.v ?? p.close);
	}

	function pointAt(plot, t) {
		let prev = null;
		for (const p of plot.data) {
			if (p.t > t) break;
			prev = p;
		}
		return prev;
	}

	function clampWindow(t0, t1) {
		const full = extent.t1 - extent.t0;
		let span = Math.min(Math.max(t1 - t0, minSpan), full);
		let s = Math.max(extent.t0, Math.min(t0, extent.t1 - span));
		return { t0: s, t1: s + span };
	}

	function xFor(t, w) {
		const span = camera ? camera.t1 - camera.t0 : extent.t1 - extent.t0;
		const t0 = camera ? camera.t0 : extent.t0;
		const inner = w - PAD.left - PAD.right - XPAD * 2;
		return PAD.left + XPAD + ((t - t0) / span) * inner;
	}

	function tFor(x, w) {
		const span = camera ? camera.t1 - camera.t0 : extent.t1 - extent.t0;
		const t0 = camera ? camera.t0 : extent.t0;
		const inner = Math.max(1, w - PAD.left - PAD.right - XPAD * 2);
		return t0 + ((x - PAD.left - XPAD) / inner) * span;
	}

	function extentX(t, w) {
		return PAD.left + ((t - extent.t0) / (extent.t1 - extent.t0)) * (w - PAD.left - PAD.right);
	}

	function extentTimeAt(x, w) {
		return (
			extent.t0 +
			((x - PAD.left) / Math.max(1, w - PAD.left - PAD.right)) * (extent.t1 - extent.t0)
		);
	}

	function rangeFor(t0, t1) {
		let lo = Infinity;
		let hi = -Infinity;
		for (const plot of plots) {
			const bars = isBars(plot);
			for (const p of plot.data) {
				if (p.t < t0 || p.t > t1) continue;
				if (bars) {
					if (p.low < lo) lo = p.low;
					if (p.high > hi) hi = p.high;
				} else {
					const v = p.v ?? p.close;
					if (v < lo) lo = v;
					if (v > hi) hi = v;
				}
			}
		}
		if (!Number.isFinite(lo) || !Number.isFinite(hi)) return null;
		if (lo === hi) {
			const d = Math.abs(lo) * 0.1 || 1;
			return { lo: lo - d, hi: hi + d };
		}
		const pad = (hi - lo) * 0.08;
		return { lo: lo - pad, hi: hi + pad };
	}

	function effectiveRange(h) {
		if (!camera || !extent) return null;
		if (fixedRange) return fixedRange;
		return rangeFor(camera.t0, camera.t1);
	}

	function plotH(h) {
		return h - PAD.top - PAD.bottom;
	}

	function yFor(v, r, h) {
		return PAD.top + plotH(h) - ((v - r.lo) / (r.hi - r.lo)) * plotH(h);
	}

	function valueAtY(y, h, r) {
		if (!r || plotH(h) <= 0) return undefined;
		return r.hi - ((y - PAD.top) / plotH(h)) * (r.hi - r.lo);
	}

	function nearestPoint(t) {
		let best = null;
		for (let pi = 0; pi < plots.length; pi++) {
			for (const p of plots[pi].data) {
				if (!best || Math.abs(p.t - t) < Math.abs(best.point.t - t)) {
					best = { pi, point: p };
				}
			}
		}
		return best;
	}

	$effect(() => {
		if (!canvas || !camera || !extent || !size) return;
		const dpr = Math.max(1, window.devicePixelRatio || 1);
		const { w, h } = size;
		canvas.width = Math.round(w * dpr);
		canvas.height = Math.round(h * dpr);
		const ctx = canvas.getContext('2d');
		ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
		ctx.fillStyle = '#0c1220';
		ctx.fillRect(0, 0, w, h);
		ctx.font = '11px system-ui, sans-serif';
		const r = effectiveRange(h);
		if (!r) return;
		drawFrame(ctx, w, h, r);
		for (const plot of plots) {
			if (plot.render === 'candles' && isBars(plot)) drawCandles(ctx, w, h, r, plot);
			else drawLine(ctx, w, h, r, plot);
		}
		drawMarkers(ctx, w, h, r);
		drawAxis(ctx, w, h);
		if (hover) drawCrosshair(ctx, w, h, r);
	});

	function drawFrame(ctx, w, h, r) {
		ctx.strokeStyle = GRID;
		ctx.fillStyle = AXIS;
		for (let i = 0; i <= 3; i++) {
			const v = r.lo + ((r.hi - r.lo) * i) / 3;
			const y = yFor(v, r, h);
			ctx.beginPath();
			ctx.moveTo(PAD.left, y);
			ctx.lineTo(w - PAD.right, y);
			ctx.stroke();
			ctx.fillText(fmtValue(v), 6, y + 4);
			ctx.fillText(fmtValue(v), w - PAD.right + 6, y + 4);
		}
		const readoutT = hover ? hover.t : camera.t1;
		let lx = PAD.left + 8;
		for (const plot of plots) {
			const bars = isBars(plot);
			const pt = pointAt(plot, readoutT);
			let seg = plot.name ?? '';
			let color = plot.color;
			if (pt) {
				if (bars) {
					color = pt.close >= pt.open ? UP : DOWN;
					seg = `${plot.name ? plot.name + ' ' : ''}O ${fmtValue(pt.open)} H ${fmtValue(pt.high)} L ${fmtValue(pt.low)} C ${fmtValue(pt.close)}`;
				} else {
					seg = `${plot.name ? plot.name + ' ' : ''}${fmtValue(plotValue(plot, pt))}`;
				}
			}
			const segW = ctx.measureText(seg).width;
			if (lx + segW > w - PAD.right - 8) break;
			ctx.fillStyle = color;
			ctx.fillText(seg, lx, PAD.top + 15);
			lx += segW + 12;
		}
	}

	function drawLine(ctx, w, h, r, plot) {
		const points = [];
		for (const p of plot.data) {
			if (p.t < camera.t0 || p.t > camera.t1) continue;
			points.push([xFor(p.t, w), yFor(plotValue(plot, p), r, h)]);
		}
		if (!points.length) return;
		if (plot.fill) {
			const bottom = h - PAD.bottom;
			const grad = ctx.createLinearGradient(0, PAD.top, 0, bottom);
			grad.addColorStop(0, withAlpha(plot.color, 0.26));
			grad.addColorStop(1, withAlpha(plot.color, 0));
			ctx.beginPath();
			ctx.moveTo(points[0][0], bottom);
			for (const [x, y] of points) ctx.lineTo(x, y);
			ctx.lineTo(points.at(-1)[0], bottom);
			ctx.closePath();
			ctx.fillStyle = grad;
			ctx.fill();
		}
		ctx.strokeStyle = plot.color;
		ctx.lineWidth = 1.6;
		ctx.beginPath();
		ctx.moveTo(points[0][0], points[0][1]);
		for (const [x, y] of points) ctx.lineTo(x, y);
		ctx.stroke();
		ctx.lineWidth = 1;
	}

	function withAlpha(hex, alpha) {
		const r = parseInt(hex.slice(1, 3), 16);
		const g = parseInt(hex.slice(3, 5), 16);
		const b = parseInt(hex.slice(5, 7), 16);
		return `rgba(${r}, ${g}, ${b}, ${alpha})`;
	}

	function drawCandles(ctx, w, h, r, plot) {
		const visible = plot.data.filter((p) => p.t >= camera.t0 && p.t <= camera.t1);
		if (visible.length === 0) return;
		const plotW = w - PAD.left - PAD.right;
		const bodyW = Math.max(1, Math.min(13, (plotW / visible.length) * 0.66));
		for (const p of visible) {
			const x = xFor(p.t, w);
			const up = p.close >= p.open;
			ctx.strokeStyle = up ? UP : DOWN;
			ctx.beginPath();
			ctx.moveTo(x, yFor(p.high, r, h));
			ctx.lineTo(x, yFor(p.low, r, h));
			ctx.stroke();
			const yOpen = yFor(p.open, r, h);
			const yClose = yFor(p.close, r, h);
			const top = Math.min(yOpen, yClose);
			const bh = Math.max(1, Math.abs(yClose - yOpen));
			if (up) {
				ctx.fillStyle = UP;
				ctx.fillRect(x - bodyW / 2, top, bodyW, bh);
			} else {
				ctx.fillStyle = DOWN;
				ctx.fillRect(x - bodyW / 2, top, bodyW, bh);
			}
		}
	}

	function drawMarkers(ctx, w, h, r) {
		for (const m of markers) {
			if (m.t < camera.t0 || m.t > camera.t1) continue;
			if (m.v === undefined || m.v === null) continue;
			triangle(ctx, xFor(m.t, w), yFor(m.v, r, h), m.dir === 'up', !m.hollow, m.color);
		}
	}

	function triangle(ctx, x, y, up, filled, color) {
		ctx.beginPath();
		if (up) {
			ctx.moveTo(x, y - 6);
			ctx.lineTo(x - 5, y + 4);
			ctx.lineTo(x + 5, y + 4);
		} else {
			ctx.moveTo(x, y + 6);
			ctx.lineTo(x - 5, y - 4);
			ctx.lineTo(x + 5, y - 4);
		}
		ctx.closePath();
		if (filled) {
			ctx.fillStyle = color;
			ctx.fill();
		} else {
			ctx.strokeStyle = color;
			ctx.lineWidth = 1.4;
			ctx.stroke();
			ctx.lineWidth = 1;
		}
	}

	function drawAxis(ctx, w, h) {
		const axisY = h - PAD.bottom;
		const span = camera.t1 - camera.t0;
		ctx.fillStyle = AXIS;
		for (let i = 0; i <= 6; i++) {
			const t = camera.t0 + (span * i) / 6;
			const x = xFor(t, w);
			ctx.fillText(axisLabel(t), Math.min(x + 3, w - PAD.right - 40), axisY + 10);
		}
		const trackY = axisY + 20;
		const plotW = w - PAD.left - PAD.right;
		ctx.fillStyle = 'rgba(139, 148, 158, 0.25)';
		ctx.fillRect(PAD.left, trackY - 2, plotW, 4);
		const wx0 = extentX(camera.t0, w);
		const wx1 = extentX(camera.t1, w);
		ctx.fillStyle = HANDLE;
		ctx.fillRect(wx0, trackY - 4, Math.max(2, wx1 - wx0), 8);
		ctx.fillStyle = ACCENT;
		ctx.fillRect(wx0 - 2, trackY - 7, 5, 14);
		ctx.fillRect(wx1 - 3, trackY - 7, 5, 14);
	}

	function fmtValue(v) {
		if (v === undefined || v === null || !Number.isFinite(v)) return '';
		return v >= 10000 ? v.toLocaleString('en-US', { maximumFractionDigits: 0 }) : v.toFixed(2);
	}

	function shortDate(t) {
		return new Date(t).toLocaleDateString('en-US', { month: 'short', year: '2-digit' });
	}

	function shortDay(t) {
		return new Date(t).toLocaleDateString('en-US', { month: 'short', day: '2-digit', year: '2-digit' });
	}

	function axisLabel(t) {
		if (axis === 'time') {
			return new Date(t).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false });
		}
		return shortDate(t);
	}

	function hoverLabel(t) {
		if (axis === 'time') {
			return new Date(t).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
		}
		return shortDay(t);
	}

	function drawCrosshair(ctx, w, h, r) {
		const { x, y, pi, point } = hover;
		const plot = plots[pi];
		ctx.strokeStyle = 'rgba(226, 232, 240, 0.35)';
		ctx.beginPath();
		ctx.moveTo(x, PAD.top);
		ctx.lineTo(x, h - PAD.bottom);
		ctx.moveTo(PAD.left, y);
		ctx.lineTo(w - PAD.right, y);
		ctx.stroke();
		ctx.fillStyle = '#0c1220';
		ctx.fillRect(w - PAD.right + 3, y - 7, PAD.right - 6, 14);
		ctx.fillStyle = '#f1f5f9';
		ctx.fillText(fmtValue(plotValue(plot, point)), w - PAD.right + 7, y + 4);
		const label = hoverLabel(hover.t);
		const tw = ctx.measureText(label).width + 10;
		const tx = Math.min(Math.max(x - tw / 2, PAD.left), w - PAD.right - tw);
		ctx.fillStyle = '#0c1220';
		ctx.fillRect(tx, h - PAD.bottom + 1, tw, 13);
		ctx.fillStyle = '#f1f5f9';
		ctx.fillText(label, tx + 5, h - PAD.bottom + 11);
		if (hover.tooltip) {
			const lines = hover.tooltip.lines;
			const bw = Math.max(...lines.map((l) => ctx.measureText(l).width)) + 16;
			const bh = 14 * lines.length + 8;
			const bx = Math.min(hover.tooltip.x + 12, w - PAD.right - bw);
			const by = Math.max(PAD.top + 2, Math.min(hover.tooltip.y - bh - 10, h - PAD.bottom - bh - 2));
			ctx.fillStyle = 'rgba(12, 18, 32, 0.95)';
			ctx.fillRect(bx, by, bw, bh);
			ctx.strokeStyle = FRAME;
			ctx.strokeRect(bx + 0.5, by + 0.5, bw - 1, bh - 1);
			ctx.fillStyle = '#f1f5f9';
			lines.forEach((l, i) => ctx.fillText(l, bx + 8, by + 18 + i * 14));
		}
	}

	function hitMode(e) {
		const rect = canvas.getBoundingClientRect();
		const x = e.clientX - rect.left;
		const y = e.clientY - rect.top;
		const axisY = rect.height - PAD.bottom;
		if (y > axisY) {
			const wx0 = extentX(camera.t0, rect.width);
			const wx1 = extentX(camera.t1, rect.width);
			if (Math.abs(x - wx0) < 8) return 'gripL';
			if (Math.abs(x - wx1) < 8) return 'gripR';
			if (x >= PAD.left && x <= rect.width - PAD.right) {
				return x < wx0 || x > wx1 ? 'scrub' : 'slide';
			}
			return null;
		}
		if (x >= PAD.left && x <= rect.width - PAD.right && y >= PAD.top && y <= axisY) return 'pan';
		return null;
	}

	function cursorFor(x, y, w, h) {
		const axisY = h - PAD.bottom;
		if (y > axisY) {
			const wx0 = extentX(camera.t0, w);
			const wx1 = extentX(camera.t1, w);
			if (Math.abs(x - wx0) < 8 || Math.abs(x - wx1) < 8) return 'ew-resize';
			if (x >= PAD.left && x <= w - PAD.right) return x < wx0 || x > wx1 ? 'pointer' : 'grab';
		}
		return 'crosshair';
	}

	function handleDown(e) {
		if (!camera || !extent) return;
		const mode = hitMode(e);
		if (!mode) return;
		const rect = canvas.getBoundingClientRect();
		const x = e.clientX - rect.left;
		if (mode === 'scrub') {
			const t = extentTimeAt(x, rect.width);
			const span = camera.t1 - camera.t0;
			const win = clampWindow(t - span / 2, t + span / 2);
			camera = { ...camera, t0: win.t0, t1: win.t1 };
		}
		drag = {
			mode,
			startX: x,
			startView: { ...camera },
			grabOffset:
				mode === 'slide' || mode === 'gripL'
					? extentTimeAt(x, rect.width) - camera.t0
					: mode === 'gripR'
						? extentTimeAt(x, rect.width) - camera.t1
						: 0
		};
		canvas.style.cursor = 'grabbing';
		canvas.setPointerCapture(e.pointerId);
	}

	function handleMove(e) {
		const rect = canvas.getBoundingClientRect();
		const x = e.clientX - rect.left;
		const y = e.clientY - rect.top;
		if (drag) {
			canvas.style.cursor = 'grabbing';
			const plotW = Math.max(1, rect.width - PAD.left - PAD.right);
			if (drag.mode === 'pan') {
				const span = drag.startView.t1 - drag.startView.t0;
				const dt = ((x - drag.startX) / plotW) * span;
				const w = clampWindow(drag.startView.t0 - dt, drag.startView.t1 - dt);
				camera = { ...camera, t0: w.t0, t1: w.t1 };
			} else if (drag.mode === 'gripL') {
				const t0 = extentTimeAt(x, rect.width) - drag.grabOffset;
				camera = {
					...camera,
					t0: Math.max(extent.t0, Math.min(t0, camera.t1 - minSpan))
				};
			} else if (drag.mode === 'gripR') {
				const t1 = extentTimeAt(x, rect.width) - drag.grabOffset;
				camera = {
					...camera,
					t1: Math.min(extent.t1, Math.max(t1, camera.t0 + minSpan))
				};
			} else if (drag.mode === 'scrub') {
				const t = extentTimeAt(x, rect.width);
				const span = drag.startView.t1 - drag.startView.t0;
				const win = clampWindow(t - span / 2, t + span / 2);
				camera = { ...camera, t0: win.t0, t1: win.t1 };
			} else if (drag.mode === 'slide') {
				const span = drag.startView.t1 - drag.startView.t0;
				const t0 = extentTimeAt(x, rect.width) - drag.grabOffset;
				const win = clampWindow(t0, t0 + span);
				camera = { ...camera, t0: win.t0, t1: win.t1 };
			}
			hover = null;
			return;
		}
		if (!camera || !extent) return;
		canvas.style.cursor = cursorFor(x, y, rect.width, rect.height);
		const plotR = rect.width - PAD.right;
		const plotB = rect.height - PAD.bottom;
		if (x < PAD.left || x > plotR || y < PAD.top || y > plotB) {
			hover = null;
			return;
		}
		const t = tFor(x, rect.width);
		const found = nearestPoint(t);
		const r = effectiveRange(rect.height);
		if (!found || !r) {
			hover = null;
			return;
		}
		const px = xFor(found.point.t, rect.width);
		const py = yFor(plotValue(plots[found.pi], found.point), r, rect.height);
		const lines = [];
		for (const m of markers) {
			if (m.t !== found.point.t || !m.label) continue;
			const ml = Array.isArray(m.label) ? m.label : [m.label];
			lines.push(...ml);
		}
		hover = {
			x: px,
			y: py,
			t: found.point.t,
			pi: found.pi,
			point: found.point,
			tooltip: lines.length ? { x: px, y: py, lines } : null
		};
	}

	function handleUp() {
		drag = null;
		canvas.style.cursor = 'crosshair';
	}

	function handleLeave() {
		hover = null;
		drag = null;
		canvas.style.cursor = 'crosshair';
	}

	function reset() {
		if (extent) camera = { t0: extent.t0, t1: extent.t1 };
		valueWindow = null;
	}
</script>

<figure class="wrap">
	{#if title}
		<figcaption><span class="tick" aria-hidden="true"></span>{title}</figcaption>
	{/if}
	<div class="holder" bind:this={holder} style="height: {height}px">
		<canvas
			bind:this={canvas}
			onpointerdown={handleDown}
			onpointermove={handleMove}
			onpointerup={handleUp}
			onpointerleave={handleLeave}
			ondblclick={reset}
		></canvas>
	</div>
</figure>

<style>
	.wrap {
		margin: 0;
		background: var(--surface-well);
		border-radius: 10px;
		overflow: hidden;
	}
	figcaption {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 7px 12px;
		font-size: 11px;
		font-weight: 600;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--muted);
	}
	.tick {
		width: 14px;
		height: 2px;
		border-radius: 1px;
		background: var(--accent);
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
</style>
