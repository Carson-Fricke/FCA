<script>
	let { nodes = [], selected = null, onselect = null, height = 560 } = $props();

	const W = 196;
	const H = 52;
	const GAP_X = 96;
	const GAP_Y = 12;

	const STATUS = {
		done: '#3fb950',
		active: '#58a6ff',
		pending: '#8b949e',
		blocked: '#f85149',
		research: '#d29922',
		experiment: '#a371f7'
	};

	let wrap;
	let canvas;
	let size = $state({ w: 960, h: 560 });
	let view = $state({ k: 1, tx: 24, ty: 24 });
	let ready = $state(false);

	const byId = $derived(Object.fromEntries(nodes.map((n) => [n.id, n])));

	const boxes = $derived.by(() => {
		const depth = {};
		const childrenOf = {};
		for (const n of nodes) childrenOf[n.parentId ?? '__root'] ??= [];
		for (const n of nodes) childrenOf[n.parentId ?? '__root'].push(n);
		const columns = [];
		const queue = childrenOf['__root'] ?? [];
		let frontier = queue;
		let d = 0;
		const seen = new Set();
		while (frontier.length) {
			columns[d] = frontier;
			const next = [];
			for (const n of frontier) {
				seen.add(n.id);
				depth[n.id] = d;
			}
			for (const n of frontier) {
				for (const c of childrenOf[n.id] ?? []) {
					if (!seen.has(c.id)) next.push(c);
				}
			}
			frontier = next;
			d += 1;
		}
		const map = {};
		for (let c = 0; c < columns.length; c++) {
			let y = 24;
			for (const n of columns[c]) {
				const h = n.type === 'task' ? H : H + 12;
				map[n.id] = { x: 24 + c * (W + GAP_X), y, w: W, h };
				y += h + GAP_Y;
			}
		}
		return map;
	});

	const bbox = $derived.by(() => {
		let x0 = Infinity;
		let y0 = Infinity;
		let x1 = -Infinity;
		let y1 = -Infinity;
		for (const b of Object.values(boxes)) {
			x0 = Math.min(x0, b.x);
			y0 = Math.min(y0, b.y);
			x1 = Math.max(x1, b.x + b.w);
			y1 = Math.max(y1, b.y + b.h);
		}
		if (!Number.isFinite(x0)) return null;
		return { x0, y0, x1, y1 };
	});

	$effect(() => {
		const cv = canvas;
		if (!cv) return;
		const ro = new ResizeObserver((entries) => {
			const r = entries[0].contentRect;
			size = { w: Math.max(320, r.width), h: Math.max(320, r.height) };
		});
		ro.observe(cv.parentElement);
		return () => ro.disconnect();
	});

	function fit() {
		if (!bbox || !size) return;
		const pad = 32;
		const bw = bbox.x1 - bbox.x0 + pad * 2;
		const bh = bbox.y1 - bbox.y0 + pad * 2;
		const k = Math.min(size.w / bw, size.h / bh, 1.25);
		view = {
			k,
			tx: (size.w - bw * k) / 2 - (bbox.x0 - pad) * k,
			ty: (size.h - bh * k) / 2 - (bbox.y0 - pad) * k
		};
	}

	$effect(() => {
		if (canvas && bbox && size.w && !ready) {
			fit();
			ready = true;
		}
	});

	$effect(() => {
		if (!canvas || !view || !size) return;
		const dpr = Math.max(1, window.devicePixelRatio || 1);
		canvas.width = Math.round(size.w * dpr);
		canvas.height = Math.round(size.h * dpr);
		const ctx = canvas.getContext('2d');
		ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
		ctx.clearRect(0, 0, size.w, size.h);
		ctx.setTransform(dpr * view.k, 0, 0, dpr * view.k, dpr * view.tx, dpr * view.ty);
		ctx.lineWidth = 1 / view.k;
		for (const n of nodes) {
			for (const dep of n.depends ?? []) {
				const from = boxes[dep];
				const to = boxes[n.id];
				if (!from || !to) continue;
				const depNode = byId[dep];
				const done = depNode?.status === 'done';
				ctx.strokeStyle = done ? 'rgba(63, 185, 80, 0.55)' : 'rgba(139, 148, 158, 0.4)';
				ctx.setLineDash(done ? [] : [4 / view.k, 4 / view.k]);
				const x0 = from.x + from.w;
				const y0 = from.y + from.h / 2;
				const x1 = to.x;
				const y1 = to.y + to.h / 2;
				const mid = (x0 + x1) / 2;
				ctx.beginPath();
				ctx.moveTo(x0, y0);
				ctx.bezierCurveTo(mid, y0, mid, y1, x1, y1);
				ctx.stroke();
				ctx.setLineDash([]);
			}
		}
		for (const n of nodes) {
			const b = boxes[n.id];
			if (!b) continue;
			const color = STATUS[n.status] ?? '#8b949e';
			const isSel = selected?.id === n.id;
			ctx.fillStyle = 'rgba(22, 27, 34, 0.92)';
			ctx.strokeStyle = isSel ? '#e3b341' : color;
			ctx.lineWidth = (isSel ? 2.2 : 1.4) / view.k;
			roundRect(ctx, b.x, b.y, b.w, b.h, 8);
			ctx.fill();
			ctx.stroke();
			ctx.fillStyle = color;
			ctx.fillRect(b.x, b.y, 4, b.h);
			ctx.fillStyle = '#e6edf3';
			ctx.font = `${n.type === 'task' ? 11 : 12}px system-ui, sans-serif`;
			ctx.fillText(clip(ctx, n.title, b.w - 20), b.x + 12, b.y + 19);
			ctx.fillStyle = '#8b949e';
			ctx.font = '10px system-ui, sans-serif';
			const depCount = (n.depends ?? []).length;
			const right = `${n.id}  ${n.status}${depCount ? `  dep:${depCount}` : ''}`;
			ctx.fillText(right, b.x + 12, b.y + b.h - 9);
		}
	});

	function clip(ctx, text, max) {
		if (ctx.measureText(text).width <= max) return text;
		let t = text;
		while (t.length > 3 && ctx.measureText(t + '…').width > max) t = t.slice(0, -1);
		return t + '…';
	}

	function roundRect(ctx, x, y, w, h, r) {
		ctx.beginPath();
		ctx.moveTo(x + r, y);
		ctx.arcTo(x + w, y, x + w, y + h, r);
		ctx.arcTo(x + w, y + h, x, y + h, r);
		ctx.arcTo(x, y + h, x, y, r);
		ctx.arcTo(x, y, x + w, y, r);
		ctx.closePath();
	}

	let dragging = false;
	let moved = 0;
	let last = null;

	function toWorld(sx, sy) {
		return { x: (sx - view.tx) / view.k, y: (sy - view.ty) / view.k };
	}

	function hit(sx, sy) {
		const p = toWorld(sx, sy);
		const list = [...nodes].reverse();
		for (const n of list) {
			const b = boxes[n.id];
			if (!b) continue;
			if (p.x >= b.x && p.x <= b.x + b.w && p.y >= b.y && p.y <= b.y + b.h) return n;
		}
		return null;
	}

	function handleDown(e) {
		const rect = canvas.getBoundingClientRect();
		dragging = true;
		moved = 0;
		last = { x: e.clientX - rect.left, y: e.clientY - rect.top };
		canvas.setPointerCapture(e.pointerId);
	}

	function handleMove(e) {
		if (!dragging) return;
		const rect = canvas.getBoundingClientRect();
		const cur = { x: e.clientX - rect.left, y: e.clientY - rect.top };
		const dx = cur.x - last.x;
		const dy = cur.y - last.y;
		moved += Math.abs(dx) + Math.abs(dy);
		view = { k: view.k, tx: view.tx + dx, ty: view.ty + dy };
		last = cur;
	}

	function handleUp(e) {
		if (!dragging) return;
		dragging = false;
		if (moved < 6) {
			const rect = canvas.getBoundingClientRect();
			const n = hit(e.clientX - rect.left, e.clientY - rect.top);
			if (n && onselect) onselect(n);
		}
	}

	function handleWheel(e) {
		e.preventDefault();
		const rect = canvas.getBoundingClientRect();
		const sx = e.clientX - rect.left;
		const sy = e.clientY - rect.top;
		const world = toWorld(sx, sy);
		const k = Math.min(3, Math.max(0.15, view.k * Math.exp(e.deltaY * 0.0015)));
		view = { k, tx: sx - world.x * k, ty: sy - world.y * k };
	}
</script>

<figure bind:this={wrap} class="wrap">
	<div class="holder" style="height: {height}px">
		<canvas
			bind:this={canvas}
			onpointerdown={handleDown}
			onpointermove={handleMove}
			onpointerup={handleUp}
			onwheel={handleWheel}
			ondblclick={fit}
		></canvas>
	</div>
	<figcaption class="legend">
		{#each Object.entries(STATUS) as [name, color]}
			<span class="key"><i style="background:{color}"></i>{name}</span>
		{/each}
		<span class="hint">wheel zoom &middot; drag pan &middot; click a node &middot; double-click fit</span>
	</figcaption>
</figure>

<style>
	.wrap {
		margin: 0;
		background: var(--panel);
		border: 1px solid var(--border);
		border-radius: 8px;
		overflow: hidden;
	}
	.holder {
		width: 100%;
	}
	canvas {
		display: block;
		width: 100%;
		height: 100%;
		cursor: grab;
	}
	canvas:active {
		cursor: grabbing;
	}
	.legend {
		display: flex;
		gap: 12px;
		flex-wrap: wrap;
		align-items: center;
		padding: 8px 12px;
		font-size: 12px;
		color: var(--muted);
		border-top: 1px solid var(--border);
		margin: 0;
	}
	.key {
		display: inline-flex;
		align-items: center;
		gap: 5px;
	}
	.key i {
		width: 9px;
		height: 9px;
		border-radius: 2px;
		display: inline-block;
	}
	.hint {
		margin-left: auto;
	}
</style>
