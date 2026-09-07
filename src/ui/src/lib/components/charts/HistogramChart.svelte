<script>
	let { values = [], height = 200, bins = 9, label = 'Trade PnL distribution' } = $props();

	const FRAME = 'rgba(148, 163, 184, 0.14)';
	const AXIS = '#64748b';
	const GREEN = '#34d399';
	const RED = '#f87171';
	const TEXT = '#94a3b8';

	let canvas;
	let size = $state(null);
	let hover = $state(null);

	const buckets = $derived.by(() => {
		const finite = values.filter((v) => Number.isFinite(v));
		if (!finite.length) return [];
		const min = Math.min(...finite);
		const max = Math.max(...finite);
		if (min === max) return [{ lo: min, hi: max, count: finite.length, positive: min >= 0 }];
		const span = max - min;
		let negBins = min < 0 ? Math.max(1, Math.round((-min / span) * bins)) : 0;
		let posBins = max > 0 ? Math.max(1, bins - negBins) : 0;
		negBins = bins - posBins;
		const edges = [];
		if (negBins) for (let i = 0; i <= negBins; i++) edges.push(min + ((0 - min) * i) / negBins);
		if (posBins) for (let i = 1; i <= posBins; i++) edges.push((max / posBins) * i);
		const counts = edges.slice(0, -1).map(() => 0);
		for (const v of finite) {
			let idx = 0;
			while (idx < edges.length - 2 && v >= edges[idx + 1]) idx++;
			counts[idx]++;
		}
		return counts.map((count, i) => ({
			lo: edges[i],
			hi: edges[i + 1],
			count,
			positive: edges[i] >= 0
		}));
	});

	const maxCount = $derived(Math.max(1, ...buckets.map((b) => b.count)));

	$effect(() => {
		const cv = canvas;
		if (!cv) return;
		const ro = new ResizeObserver((entries) => {
			const r = entries[0].contentRect;
			size = { w: r.width, h: r.height };
		});
		ro.observe(cv);
		return () => ro.disconnect();
	});

	$effect(() => {
		if (!canvas || !size || !buckets.length) return;
		const dpr = Math.max(1, window.devicePixelRatio || 1);
		const { w, h } = size;
		canvas.width = Math.round(w * dpr);
		canvas.height = Math.round(h * dpr);
		const ctx = canvas.getContext('2d');
		ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
		ctx.fillStyle = '#0c1220';
		ctx.fillRect(0, 0, w, h);
		ctx.font = '11px system-ui, sans-serif';
		const pad = { top: 8, right: 8, bottom: 24, left: 8 };
		const plotW = w - pad.left - pad.right;
		const plotH = h - pad.top - pad.bottom;
		const bw = plotW / buckets.length;
		for (let i = 0; i < buckets.length; i++) {
			const b = buckets[i];
			const x = pad.left + i * bw;
			const bh = (b.count / maxCount) * (plotH - 4);
			const y = pad.top + plotH - bh;
			ctx.fillStyle = b.positive ? GREEN : RED;
			ctx.globalAlpha = hover && hover.index !== i ? 0.45 : 0.9;
			roundRect(ctx, x + 2, y, bw - 4, Math.max(2, bh), 4);
			ctx.fill();
			ctx.globalAlpha = 1;
		}
		ctx.strokeStyle = FRAME;
		ctx.beginPath();
		ctx.moveTo(pad.left, pad.top + plotH + 0.5);
		ctx.lineTo(pad.left + plotW, pad.top + plotH + 0.5);
		ctx.stroke();
		ctx.fillStyle = AXIS;
		ctx.textAlign = 'center';
		ctx.fillText(fmt(buckets[0].lo), pad.left + bw / 2, h - 8);
		const zeroX = zeroIndexX(buckets, pad.left, bw);
		if (zeroX !== null) ctx.fillText('0', zeroX, h - 8);
		ctx.fillText(fmt(buckets.at(-1).hi), pad.left + (buckets.length - 0.5) * bw, h - 8);
		ctx.textAlign = 'left';
	});

	function zeroIndexX(list, left, bw) {
		const idx = list.findIndex((b, i) => b.lo < 0 && b.hi >= 0);
		return idx === -1 ? null : left + (idx + 0.5) * bw;
	}

	function roundRect(ctx, x, y, w, h, r) {
		const rad = Math.min(r, w / 2, h / 2);
		ctx.beginPath();
		ctx.moveTo(x + rad, y);
		ctx.arcTo(x + w, y, x + w, y + h, rad);
		ctx.arcTo(x + w, y + h, x, y + h, rad);
		ctx.arcTo(x, y + h, x, y, rad);
		ctx.arcTo(x, y, x + w, y, rad);
		ctx.closePath();
	}

	function fmt(v) {
		return Math.abs(v) >= 1000 ? `${(v / 1000).toFixed(1)}k` : v.toFixed(0);
	}

	function money(v) {
		return `${v >= 0 ? '+' : '-'}${Math.abs(v).toLocaleString('en-US', { maximumFractionDigits: 0 })}`;
	}

	function handleMove(e) {
		if (!canvas || !size || !buckets.length) return;
		const rect = canvas.getBoundingClientRect();
		const x = e.clientX - rect.left;
		const pad = { left: 8, right: 8 };
		const plotW = size.w - pad.left - pad.right;
		const idx = Math.floor(((x - pad.left) / plotW) * buckets.length);
		hover = idx >= 0 && idx < buckets.length ? { index: idx, x: e.clientX - rect.left, y: e.clientY - rect.top } : null;
	}

	function handleLeave() {
		hover = null;
	}
</script>

<figure class="wrap">
	<figcaption>
		<span class="tick" aria-hidden="true"></span>
		<span>{label}</span>
		{#if hover}
			<span class="tip" style="left: {hover.x}px">
				{money(buckets[hover.index].lo)} &rarr; {money(buckets[hover.index].hi)}
				<b>{buckets[hover.index].count} trades</b>
			</span>
		{/if}
	</figcaption>
	<div class="holder" style="height: {height}px">
		<canvas
			bind:this={canvas}
			onpointermove={handleMove}
			onpointerleave={handleLeave}
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
		position: relative;
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
	.tip {
		position: absolute;
		top: 6px;
		margin-left: 10px;
		color: var(--text);
		font-size: 11px;
		font-weight: 400;
		text-transform: none;
		letter-spacing: normal;
		white-space: nowrap;
	}
	.tip b {
		color: var(--accent);
		font-weight: 600;
		margin-left: 8px;
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
