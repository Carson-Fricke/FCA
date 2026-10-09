<script lang="ts">
	import {
		ACTIVE_STATES,
		FAILED_STATES,
		SUCCEEDED_STATES,
		runStateTone,
		type ParameterBarCategory
	} from './parameter-bar';

	/**
	 * Grouped canvas bars for one varied parameter: the parameter level on the
	 * X axis, total return and profit factor on the Y axes (each series keeps
	 * its own scale, labeled on its own side; raw values stay readable in the
	 * companion table). Missing metrics draw a hatched gap marker, never a
	 * zero-height bar, and non-succeeded runs draw a distinct state marker.
	 */
	let {
		title = '',
		categories = [],
		height = 200,
		testId = 'parameter-bar-chart'
	}: {
		title?: string;
		categories?: ParameterBarCategory[];
		height?: number;
		testId?: string;
	} = $props();

	let FRAME = '#e5eae6';
	let AXIS = '#6b7a67';
	let TEXT = '#242d29';
	let BG = '#ffffff';
	let RETURN = '#2a8761';
	let FACTOR = '#8a74af';
	let CENTER = 'rgba(42, 135, 97, 0.10)';
	let LOSS = '#c76868';
	let WARN = '#d6ad58';
	const CANVAS_FONT = '11px "SFMono-Regular", "SF Mono", Consolas, ui-monospace, monospace';

	function theme(canvas: HTMLCanvasElement, name: string, fallback: string): string {
		try {
			const value = getComputedStyle(canvas).getPropertyValue(name).trim();
			return value || fallback;
		} catch {
			return fallback;
		}
	}

	function applyCanvasTheme(canvas: HTMLCanvasElement): void {
		BG = theme(canvas, '--surface-900', BG);
		FRAME = theme(canvas, '--border', FRAME);
		AXIS = theme(canvas, '--muted', AXIS);
		TEXT = theme(canvas, '--text', TEXT);
		RETURN = theme(canvas, '--color-profit', RETURN);
		FACTOR = theme(canvas, '--purple', FACTOR);
		LOSS = theme(canvas, '--color-loss', LOSS);
		WARN = theme(canvas, '--color-warn', WARN);
	}

	let canvas = $state<HTMLCanvasElement | undefined>();
	let size = $state<{ w: number; h: number } | null>(null);

	$effect(() => {
		const element = canvas;
		if (!element) return;
		const observer = new ResizeObserver((entries) => {
			const rect = entries[0].contentRect;
			size = { w: rect.width, h: rect.height };
		});
		observer.observe(element);
		return () => observer.disconnect();
	});

	$effect(() => {
		const element = canvas;
		if (!element || !size || !categories.length) return;
		const context = element.getContext('2d');
		if (!context) return;
		draw(context, element, size.w, size.h, categories);
	});

	function rangeFor(values: Array<number | null>): { lo: number; hi: number } | null {
		const finite = values.filter((value): value is number => typeof value === 'number' && Number.isFinite(value));
		if (!finite.length) return null;
		let lo = Math.min(0, ...finite);
		let hi = Math.max(0, ...finite);
		if (lo === hi) {
			if (lo === 0) return { lo: 0, hi: 1 };
			const pad = Math.abs(lo) * 0.5 || 1;
			lo -= pad;
			hi += pad;
		} else {
			const pad = (hi - lo) * 0.08;
			lo -= pad;
			hi += pad;
		}
		return { lo, hi };
	}

	function yFor(value: number, range: { lo: number; hi: number }, top: number, plotH: number): number {
		return top + plotH - ((value - range.lo) / (range.hi - range.lo)) * plotH;
	}

	function formatTick(value: number): string {
		const magnitude = Math.abs(value);
		if (magnitude >= 1000) return `${(value / 1000).toFixed(1)}k`;
		if (magnitude >= 1) return value.toFixed(1);
		return value.toFixed(2);
	}

	function draw(
		context: CanvasRenderingContext2D,
		element: HTMLCanvasElement,
		width: number,
		heightValue: number,
		items: ParameterBarCategory[]
	): void {
		applyCanvasTheme(element);
		const dpr = Math.max(1, window.devicePixelRatio || 1);
		element.width = Math.round(width * dpr);
		element.height = Math.round(heightValue * dpr);
		context.setTransform(dpr, 0, 0, dpr, 0, 0);
		context.font = CANVAS_FONT;
		context.fillStyle = BG;
		context.fillRect(0, 0, width, heightValue);
		const pad = { top: 26, right: 46, bottom: 26, left: 46 };
		const plotW = Math.max(1, width - pad.left - pad.right);
		const plotH = Math.max(1, heightValue - pad.top - pad.bottom);
		const returnRange = rangeFor(items.map((item) => item.totalReturn));
		const factorRange = rangeFor(items.map((item) => item.profitFactor));

		const ticks = 4;
		for (let index = 0; index <= ticks; index++) {
			const y = pad.top + (plotH * index) / ticks;
			context.strokeStyle = FRAME;
			context.beginPath();
			context.moveTo(pad.left, y + 0.5);
			context.lineTo(pad.left + plotW, y + 0.5);
			context.stroke();
			const fraction = 1 - index / ticks;
			context.fillStyle = AXIS;
			if (returnRange) {
				context.textAlign = 'right';
				context.fillText(
					formatTick(returnRange.lo + (returnRange.hi - returnRange.lo) * fraction),
					pad.left - 6,
					y + 4
				);
			}
			if (factorRange) {
				context.textAlign = 'left';
				context.fillText(
					formatTick(factorRange.lo + (factorRange.hi - factorRange.lo) * fraction),
					pad.left + plotW + 6,
					y + 4
				);
			}
		}

		const bandW = plotW / items.length;
		const groupW = Math.max(4, bandW * 0.64);
		const barW = Math.max(2, groupW / 2 - 2);
		for (let index = 0; index < items.length; index++) {
			const item = items[index];
			const bandX = pad.left + index * bandW;
			const groupX = bandX + (bandW - groupW) / 2;
			const centerX = groupX + groupW / 2;
			if (item.center) {
				context.fillStyle = CENTER;
				context.fillRect(bandX + 1, pad.top, bandW - 2, plotH);
			}
			drawMetricBar(context, groupX, barW, item.totalReturn, returnRange, pad.top, plotH, RETURN);
			drawMetricBar(context, groupX + barW + 4, barW, item.profitFactor, factorRange, pad.top, plotH, FACTOR);
			drawStateMarker(context, centerX, pad.top + 2, item);
			if (item.center) {
				context.strokeStyle = RETURN;
				context.lineWidth = 2;
				context.beginPath();
				context.moveTo(bandX + 3, pad.top + plotH + 1.5);
				context.lineTo(bandX + bandW - 3, pad.top + plotH + 1.5);
				context.stroke();
				context.lineWidth = 1;
			}
			context.fillStyle = TEXT;
			context.textAlign = 'center';
			context.fillText(item.label, bandX + bandW / 2, heightValue - 8, Math.max(20, bandW - 6));
		}
		context.textAlign = 'left';
	}

	function drawMetricBar(
		context: CanvasRenderingContext2D,
		x: number,
		barWidth: number,
		value: number | null,
		range: { lo: number; hi: number } | null,
		top: number,
		plotH: number,
		color: string
	): void {
		if (value === null || !Number.isFinite(value)) {
			drawGapMarker(context, x, barWidth, range ? yFor(0, range, top, plotH) : top + plotH);
			return;
		}
		const zeroY = yFor(0, range ?? { lo: 0, hi: 1 }, top, plotH);
		const valueY = yFor(value, range ?? { lo: 0, hi: 1 }, top, plotH);
		const barTop = Math.min(zeroY, valueY);
		const barHeight = Math.max(2, Math.abs(valueY - zeroY));
		context.fillStyle = color;
		context.globalAlpha = 0.9;
		context.fillRect(x, barTop, barWidth, barHeight);
		context.globalAlpha = 1;
	}

	/** Explicit missing-metric marker: an outlined, hatched gap, never zero. */
	function drawGapMarker(
		context: CanvasRenderingContext2D,
		x: number,
		barWidth: number,
		baseline: number
	): void {
		const markerHeight = 12;
		const top = baseline - markerHeight;
		context.strokeStyle = AXIS;
		context.globalAlpha = 0.75;
		context.strokeRect(x + 0.5, top + 0.5, barWidth - 1, markerHeight - 1);
		context.beginPath();
		for (let offset = 2; offset < barWidth + markerHeight; offset += 4) {
			const x0 = Math.max(x + 1, x + offset - markerHeight);
			const x1 = Math.min(x + barWidth - 1, x + offset);
			if (x1 <= x0) continue;
			context.moveTo(x0, top + (x0 - x - offset + markerHeight));
			context.lineTo(x1, top + (x1 - x - offset + markerHeight));
		}
		context.stroke();
		context.globalAlpha = 1;
	}

	function drawStateMarker(
		context: CanvasRenderingContext2D,
		centerX: number,
		y: number,
		item: ParameterBarCategory
	): void {
		const state = (item.state ?? '').toLowerCase();
		if (state === '' || (SUCCEEDED_STATES as readonly string[]).includes(state)) return;
		if ((FAILED_STATES as readonly string[]).includes(state)) {
			context.strokeStyle = LOSS;
			context.lineWidth = 1.6;
			context.beginPath();
			context.moveTo(centerX - 4, y);
			context.lineTo(centerX + 4, y + 8);
			context.moveTo(centerX + 4, y);
			context.lineTo(centerX - 4, y + 8);
			context.stroke();
			context.lineWidth = 1;
			return;
		}
		if ((ACTIVE_STATES as readonly string[]).includes(state)) {
			context.strokeStyle = RETURN;
			context.strokeRect(centerX - 4.5, y + 0.5, 9, 7);
			return;
		}
		context.fillStyle = WARN;
		context.textAlign = 'center';
		context.fillText('?', centerX, y + 8);
		context.textAlign = 'left';
	}

	function formatMetric(value: number): string {
		if (value === 0) return '0';
		const magnitude = Math.abs(value);
		if (magnitude < 0.0001) return value.toExponential(2);
		if (magnitude < 1) return value.toFixed(4);
		return value.toFixed(2);
	}
</script>

<figure class="wrap" data-testid={testId}>
	<figcaption>
		<span class="tick" aria-hidden="true"></span>
		<span class="title">{title}</span>
		<span class="legend" data-testid="{testId}-legend">
			<span class="legend-item">
				<span class="swatch return" aria-hidden="true"></span>Total return
			</span>
			<span class="legend-item">
				<span class="swatch factor" aria-hidden="true"></span>Profit factor
			</span>
		</span>
	</figcaption>
	{#if categories.length}
		<div class="holder" style="height: {height}px">
			<canvas bind:this={canvas}></canvas>
		</div>
		<table class="values" data-testid="{testId}-values">
			<thead>
				<tr>
					<th scope="col">Level</th>
					<th scope="col">Total return</th>
					<th scope="col">Profit factor</th>
					<th scope="col">State</th>
				</tr>
			</thead>
			<tbody>
				{#each categories as category (category.key)}
					<tr
						data-testid="parameter-bar-row"
						data-level={category.label}
						data-center={category.center}
						data-state={category.state ?? ''}
					>
						<td>
							<span class="level">{category.label}</span>
							{#if category.center}
								<span class="center-badge" data-testid="parameter-bar-center">center</span>
							{/if}
						</td>
						<td
							class="metric {category.totalReturn === null ? 'missing' : ''}"
							data-testid="parameter-bar-total-return"
							data-missing={category.totalReturn === null}
						>
							{category.totalReturn === null ? category.totalReturnGap ?? 'missing' : formatMetric(category.totalReturn)}
						</td>
						<td
							class="metric {category.profitFactor === null ? 'missing' : ''}"
							data-testid="parameter-bar-profit-factor"
							data-missing={category.profitFactor === null}
						>
							{category.profitFactor === null ? category.profitFactorGap ?? 'missing' : formatMetric(category.profitFactor)}
						</td>
						<td class="state {runStateTone(category.state)}" data-testid="parameter-bar-state">
							{category.state ?? 'unknown'}
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{:else}
		<p class="empty" data-testid="{testId}-empty">No levels recorded for this parameter.</p>
	{/if}
</figure>

<style>
	.wrap {
		margin: 0;
		background: var(--panel);
		border: 1px solid var(--border);
		border-radius: 10px;
		overflow: hidden;
		box-shadow: var(--shadow-card);
	}
	figcaption {
		position: relative;
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 8px;
		padding: 7px 12px;
		font-size: 11px;
		font-weight: 600;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--muted);
		border-bottom: 1px solid var(--border);
		background: var(--surface-well);
	}
	.tick {
		width: 14px;
		height: 2px;
		border-radius: 1px;
		background: var(--accent);
	}
	.title {
		font-family: var(--font-mono);
		text-transform: none;
		letter-spacing: normal;
		color: var(--text);
	}
	.legend {
		display: flex;
		flex-wrap: wrap;
		gap: 10px;
		margin-left: auto;
		font-size: 10px;
		font-weight: 500;
		text-transform: none;
		letter-spacing: normal;
		color: var(--text);
	}
	.legend-item {
		display: inline-flex;
		align-items: center;
		gap: 4px;
	}
	.swatch {
		width: 9px;
		height: 9px;
		border-radius: 2px;
	}
	.swatch.return {
		background: var(--color-profit);
	}
	.swatch.factor {
		background: var(--purple);
	}
	.holder {
		position: relative;
		width: 100%;
	}
	canvas {
		display: block;
		width: 100%;
		height: 100%;
	}
	.values {
		width: 100%;
		border-collapse: collapse;
		font-family: var(--font-mono);
		font-size: 10px;
	}
	.values th {
		padding: 4px 8px;
		text-align: right;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--muted);
		border-top: 1px solid var(--border);
	}
	.values th:first-child,
	.values td:first-child {
		text-align: left;
	}
	.values td {
		padding: 3px 8px;
		text-align: right;
		color: var(--text);
		border-top: 1px solid var(--border);
	}
	.values tr:first-child td {
		border-top: none;
	}
	.values .level {
		font-weight: 600;
	}
	.values .metric.missing {
		font-style: italic;
		color: var(--faint);
	}
	.center-badge {
		margin-left: 6px;
		padding: 0 4px;
		border-radius: 999px;
		border: 1px solid var(--border-strong);
		background: var(--surface-well);
		font-size: 9px;
		font-weight: 600;
		text-transform: uppercase;
		color: var(--accent);
	}
	.state {
		font-weight: 600;
		text-transform: uppercase;
	}
	.empty {
		margin: 0;
		padding: 10px 12px;
		font-size: 11px;
		color: var(--muted);
	}
</style>
