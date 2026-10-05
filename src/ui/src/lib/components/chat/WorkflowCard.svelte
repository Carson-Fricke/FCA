<script lang="ts">
	import { page } from '$app/state';
	import type { WorkflowDefinition, WorkflowExecutionRecord, WorkflowPhase } from '$lib/types';
	import { getWorkflowDefinition } from '$lib/api';
	import {
		computeWorkflowGraph,
		canonicalPhaseName,
		NODE_WIDTH,
		NODE_HEIGHT,
		type GraphNode,
		type WorkflowGraphModel
	} from '$lib/workflow-graph';

	/**
	 * Transcript item of kind `workflow`.
	 *
	 * Renders an interactive HTML5 Canvas with pan and zoom showing the phase
	 * transition graph (including forward flow and loopbacks). Preserves
	 * click-to-drill into phase behavior: clicking the card body enters the
	 * current active orchestrator; clicking any non-pending node drills into
	 * that phase's governing sub-agent (read-only when finished).
	 */
	let {
		execution = null,
		phases = [],
		status = 'running',
		summary = '',
		attentionPhases = [],
		onEnterPhase = () => {}
	}: {
		execution?: WorkflowExecutionRecord | null;
		phases?: WorkflowPhase[];
		status?: string;
		summary?: string;
		/** Phases whose governing agent is blocked waiting on operator input. */
		attentionPhases?: string[];
		onEnterPhase?: (phase: string, options: { readOnly: boolean }) => void;
	} = $props();

	const TONE: Record<string, string> = {
		running: 'border-accent/40 bg-accent/10 text-accent',
		launched: 'border-accent/40 bg-accent/10 text-accent',
		started: 'border-accent/40 bg-accent/10 text-accent',
		completed: 'border-profit/40 bg-profit/10 text-profit',
		failed: 'border-loss/40 bg-loss/10 text-loss',
		cancelled: 'border-loss/40 bg-loss/10 text-loss',
		paused: 'border-warn/40 bg-warn/10 text-warn',
		blocked: 'border-warn/40 bg-warn/10 text-warn'
	};

	const currentPhase = $derived(
		canonicalPhaseName(
			phases.find((phase) => phase.status === 'active')?.name ??
				execution?.phase ??
				[...phases].reverse().find((phase) => phase.status === 'done')?.name ??
				'orchestrator'
		)
	);

	/** Attention phases on their canonical names, so retired wire values still label. */
	const attentionPhaseNames = $derived(attentionPhases.map(canonicalPhaseName));

	let definition = $state<WorkflowDefinition | null>(null);
	let definitionError = $state('');
	$effect(() => {
		const id = execution?.workflow_id;
		const version = execution?.workflow_version;
		const workspaceId = execution?.workspace_id ?? page.params?.workspace ?? '';
		let cancelled = false;
		definition = null;
		definitionError = '';
		if (!id) return;
		void getWorkflowDefinition(id, { workspaceId, version }).then((value) => {
			if (cancelled) return;
			if (version && value.version !== version) {
				definitionError = `Topology unavailable for recorded version ${version}; API exposes ${value.version}.`;
				return;
			}
			definition = value;
		}).catch((error) => {
			if (!cancelled) definitionError = error instanceof Error ? error.message : 'Workflow definition unavailable.';
		});
		return () => { cancelled = true; };
	});
	const title = $derived(definition?.title || execution?.workflow_id || 'Workflow execution');

	const graphModel = $derived.by<WorkflowGraphModel>(() => {
		return computeWorkflowGraph(phases, definition);
	});

	// Canvas & Pan/Zoom state
	let canvasEl = $state<HTMLCanvasElement | null>(null);
	let containerEl = $state<HTMLDivElement | null>(null);

	let panX = $state(20);
	let panY = $state(16);
	let zoom = $state(1.0);
	let isDragging = $state(false);
	let dragMoved = $state(false);
	let startPointerX = 0;
	let startPointerY = 0;
	let hoveredNode = $state<string | null>(null);
	let animFrameId: number | null = null;
	let initialFitDone = false;
	let inViewport = $state(false);
	let documentVisible = $state(true);

	function getPointerPos(e: MouseEvent | TouchEvent): { x: number; y: number } {
		if ('touches' in e && e.touches.length > 0) {
			return { x: e.touches[0].clientX, y: e.touches[0].clientY };
		}
		const me = e as MouseEvent;
		return { x: me.clientX, y: me.clientY };
	}

	function findNodeAt(canvasX: number, canvasY: number): GraphNode | null {
		for (const node of graphModel.nodes) {
			if (
				canvasX >= node.x &&
				canvasX <= node.x + node.width &&
				canvasY >= node.y &&
				canvasY <= node.y + node.height
			) {
				return node;
			}
		}
		return null;
	}

	function fitGraph() {
		if (!containerEl || !graphModel.width || !graphModel.height) return;
		const rect = containerEl.getBoundingClientRect();
		const w = rect.width || 600;
		const h = rect.height || 260;

		const scaleX = (w - 36) / graphModel.width;
		const scaleY = (h - 32) / graphModel.height;
		const targetZoom = Math.min(1.0, Math.max(0.5, Math.min(scaleX, scaleY)));

		zoom = Number(targetZoom.toFixed(2));
		panX = Math.max(16, Math.round((w - graphModel.width * zoom) / 2));
		panY = Math.max(12, Math.round((h - graphModel.height * zoom) / 2));
		redraw();
	}

	function zoomIn() {
		applyZoom(Math.min(2.2, Number((zoom * 1.2).toFixed(2))));
	}

	function zoomOut() {
		applyZoom(Math.max(0.4, Number((zoom / 1.2).toFixed(2))));
	}

	function applyZoom(newZoom: number, centerClientX?: number, centerClientY?: number) {
		if (!containerEl) return;
		const rect = containerEl.getBoundingClientRect();
		const cx = centerClientX !== undefined ? centerClientX - rect.left : (rect.width || 600) / 2;
		const cy = centerClientY !== undefined ? centerClientY - rect.top : (rect.height || 260) / 2;

		panX = Math.round(cx - (cx - panX) * (newZoom / zoom));
		panY = Math.round(cy - (cy - panY) * (newZoom / zoom));
		zoom = newZoom;
		redraw();
	}

	function handleWheel(e: WheelEvent) {
		e.preventDefault();
		const factor = e.deltaY < 0 ? 1.12 : 0.88;
		const nextZoom = Math.min(2.2, Math.max(0.4, Number((zoom * factor).toFixed(2))));
		applyZoom(nextZoom, e.clientX, e.clientY);
	}

	function handlePointerDown(e: MouseEvent | TouchEvent) {
		const pos = getPointerPos(e);
		isDragging = true;
		dragMoved = false;
		startPointerX = pos.x - panX;
		startPointerY = pos.y - panY;
	}

	function handlePointerMove(e: MouseEvent | TouchEvent) {
		const pos = getPointerPos(e);
		if (isDragging) {
			const currentPanX = pos.x - startPointerX;
			const currentPanY = pos.y - startPointerY;
			if (Math.hypot(currentPanX - panX, currentPanY - panY) > 4) {
				dragMoved = true;
			}
			panX = currentPanX;
			panY = currentPanY;
			redraw();
			return;
		}

		if (!containerEl) return;
		const rect = containerEl.getBoundingClientRect();
		const canvasX = (pos.x - rect.left - panX) / zoom;
		const canvasY = (pos.y - rect.top - panY) / zoom;
		const node = findNodeAt(canvasX, canvasY);
		const newHover = node ? node.name : null;
		if (newHover !== hoveredNode) {
			hoveredNode = newHover;
			redraw();
		}
	}

	function handlePointerUp() {
		if (!isDragging) return;
		isDragging = false;
	}

	function handleCanvasClick(e: MouseEvent) {
		if (dragMoved || !containerEl) return;
		const rect = containerEl.getBoundingClientRect();
		const canvasX = (e.clientX - rect.left - panX) / zoom;
		const canvasY = (e.clientY - rect.top - panY) / zoom;
		const node = findNodeAt(canvasX, canvasY);
		if (node && node.status !== 'pending') {
			onEnterPhase(node.name, { readOnly: node.status === 'done' });
		}
	}

	function handleKeyDown(e: KeyboardEvent) {
		if (e.key === '+' || e.key === '=') {
			zoomIn();
		} else if (e.key === '-' || e.key === '_') {
			zoomOut();
		} else if (e.key === 'ArrowLeft') {
			panX += 24;
			redraw();
		} else if (e.key === 'ArrowRight') {
			panX -= 24;
			redraw();
		} else if (e.key === 'ArrowUp') {
			panY += 24;
			redraw();
		} else if (e.key === 'ArrowDown') {
			panY -= 24;
			redraw();
		}
	}

	function handleNodeClick(nodeName: string, isDone: boolean) {
		if (dragMoved) return;
		onEnterPhase(nodeName, { readOnly: isDone });
	}

	// Canvas Rendering Helpers
	function drawArrow(
		ctx: CanvasRenderingContext2D,
		toX: number,
		toY: number,
		angle: number,
		color: string
	) {
		const headlen = 7;
		ctx.save();
		ctx.fillStyle = color;
		ctx.beginPath();
		ctx.moveTo(toX, toY);
		ctx.lineTo(
			toX - headlen * Math.cos(angle - Math.PI / 6),
			toY - headlen * Math.sin(angle - Math.PI / 6)
		);
		ctx.lineTo(
			toX - headlen * Math.cos(angle + Math.PI / 6),
			toY - headlen * Math.sin(angle + Math.PI / 6)
		);
		ctx.closePath();
		ctx.fill();
		ctx.restore();
	}

	function redraw() {
		if (!canvasEl || !containerEl || typeof canvasEl.getContext !== 'function') return;
		const ctx = canvasEl.getContext('2d');
		if (
			!ctx ||
			typeof ctx.scale !== 'function' ||
			typeof ctx.clearRect !== 'function' ||
			typeof ctx.save !== 'function' ||
			typeof ctx.restore !== 'function'
		) {
			return;
		}

		const dpr = typeof window !== 'undefined' ? window.devicePixelRatio || 1 : 1;
		const width = containerEl.clientWidth || 600;
		const height = containerEl.clientHeight || 260;

		if (canvasEl.width !== width * dpr || canvasEl.height !== height * dpr) {
			canvasEl.width = width * dpr;
			canvasEl.height = height * dpr;
		}

		ctx.save();
		ctx.scale(dpr, dpr);
		ctx.clearRect(0, 0, width, height);

		// Background dot grid
		const gridSize = 24 * zoom;
		if (gridSize >= 8) {
			const startX = ((panX % gridSize) + gridSize) % gridSize;
			const startY = ((panY % gridSize) + gridSize) % gridSize;
			ctx.fillStyle = '#e5eae6';
			for (let x = startX; x < width; x += gridSize) {
				for (let y = startY; y < height; y += gridSize) {
					ctx.fillRect(x - 0.75, y - 0.75, 1.5, 1.5);
				}
			}
		}

		// Apply pan and zoom transform
		ctx.translate(panX, panY);
		ctx.scale(zoom, zoom);

		const pulse = (Math.sin(Date.now() / 250) + 1) / 2;

		// 1. Draw Stages (Group Boxes & Headers)
		for (const stage of graphModel.stages) {
			ctx.save();
			if (typeof ctx.setLineDash === 'function') {
				ctx.setLineDash([4, 4]);
			}
			ctx.strokeStyle = '#dce3dd';
			ctx.lineWidth = 1;
			ctx.fillStyle = 'rgba(255, 255, 255, 0.45)';
			ctx.beginPath();
			if (typeof ctx.roundRect === 'function') {
				ctx.roundRect(stage.x, stage.y, stage.width, stage.height, 12);
			} else {
				ctx.rect(stage.x, stage.y, stage.width, stage.height);
			}
			ctx.fill();
			ctx.stroke();

			// Header label with ample clearance above nodes
			ctx.font = '600 11px SFMono-Regular, Consolas, monospace';
			ctx.fillStyle = '#6b7a67';
			ctx.textAlign = 'left';
			ctx.textBaseline = 'middle';
			ctx.fillText(stage.name.toUpperCase(), stage.x + 16, stage.headerY);
			ctx.restore();
		}

		// 2. Draw Edges (Forward, Loopback, Branch)
		for (const edge of graphModel.edges) {
			ctx.save();
			const isLoopback = edge.kind === 'loopback';
			const isBranch = edge.kind === 'branch';
			const strokeColor = isLoopback
				? '#d6ad58'
				: isBranch
					? '#c76868'
					: edge.highlight
						? '#2a8761'
						: '#cbd5e1';

			ctx.strokeStyle = strokeColor;
			ctx.lineWidth = edge.highlight ? 2 : 1.5;
			if (isLoopback && typeof ctx.setLineDash === 'function') {
				ctx.setLineDash([5, 3]);
			}

			// Render Path
			if (typeof Path2D !== 'undefined') {
				ctx.stroke(new Path2D(edge.pathD));
			}

			// Arrowheads
			if (edge.arrow) {
				drawArrow(ctx, edge.arrow.x, edge.arrow.y, edge.arrow.angle, strokeColor);
			} else {
				// Forward horizontal edges point right
				const match = edge.pathD.match(/L\s+([0-9.]+)\s+([0-9.]+)/);
				if (match) {
					drawArrow(ctx, parseFloat(match[1]), parseFloat(match[2]), 0, strokeColor);
				}
			}

			// Edge Label Pills
			if (edge.label && edge.labelX !== undefined && edge.labelY !== undefined) {
				ctx.save();
				ctx.font = '600 8px SFMono-Regular, Consolas, monospace';
				const textWidth = ctx.measureText(edge.label).width;
				const pillW = textWidth + 14;
				const pillH = 14;
				const pillX = edge.labelX - pillW / 2;
				const pillY = edge.labelY - pillH / 2;

				ctx.fillStyle = '#ffffff';
				ctx.strokeStyle = '#e2e8f0';
				ctx.lineWidth = 1;
				ctx.beginPath();
				if (typeof ctx.roundRect === 'function') {
					ctx.roundRect(pillX, pillY, pillW, pillH, 7);
				} else {
					ctx.rect(pillX, pillY, pillW, pillH);
				}
				ctx.fill();
				ctx.stroke();

				ctx.fillStyle = isLoopback
					? '#b45309'
					: isBranch
						? '#c76868'
						: '#64748b';
				ctx.textAlign = 'center';
				ctx.textBaseline = 'middle';
				ctx.fillText(edge.label, edge.labelX, edge.labelY);
				ctx.restore();
			}
			ctx.restore();
		}

		// 3. Draw Nodes
		for (const node of graphModel.nodes) {
			const isAttention = attentionPhaseNames.includes(node.name);
			const isHovered = hoveredNode === node.name;
			const isActive = node.status === 'active';
			const isDone = node.status === 'done';
			const isPending = node.status === 'pending';

			ctx.save();

			// Attention pulse glow
			if (isAttention) {
				ctx.strokeStyle = `rgba(245, 158, 11, ${0.4 + pulse * 0.4})`;
				ctx.lineWidth = 4;
				ctx.beginPath();
				if (typeof ctx.roundRect === 'function') {
					ctx.roundRect(node.x - 2, node.y - 2, node.width + 4, node.height + 4, 10);
				}
				ctx.stroke();
			}

			// Box background & border
			let fill = 'rgba(255, 255, 255, 0.9)';
			let stroke = '#e2e8f0';
			let lineWidth = 1;

			if (isAttention) {
				fill = '#fffbeb';
				stroke = '#d97706';
				lineWidth = 2;
			} else if (isActive) {
				fill = '#fef3c7';
				stroke = '#d97706';
				lineWidth = isHovered ? 2.5 : 2;
			} else if (isDone) {
				fill = '#ecfdf5';
				stroke = isHovered ? '#059669' : 'rgba(42, 135, 97, 0.6)';
				lineWidth = isHovered ? 2 : 1.5;
			} else if (isPending) {
				fill = 'rgba(248, 250, 249, 0.6)';
				stroke = '#e2e8f0';
				lineWidth = 1;
			}

			if (isHovered && !isPending) {
				ctx.shadowColor = 'rgba(0, 0, 0, 0.08)';
				ctx.shadowBlur = 6;
				ctx.shadowOffsetY = 2;
			}

			ctx.fillStyle = fill;
			ctx.strokeStyle = stroke;
			ctx.lineWidth = lineWidth;
			ctx.beginPath();
			if (typeof ctx.roundRect === 'function') {
				ctx.roundRect(node.x, node.y, node.width, node.height, 8);
			} else {
				ctx.rect(node.x, node.y, node.width, node.height);
			}
			ctx.fill();
			ctx.stroke();
			ctx.restore();

			// Status Dot
			ctx.save();
			const dotX = node.x + 18;
			const dotY = node.y + node.height / 2;

			if (isActive) {
				// Outer pulsing ring
				ctx.fillStyle = `rgba(217, 119, 6, ${0.25 + pulse * 0.25})`;
				ctx.beginPath();
				ctx.arc(dotX, dotY, 4.5 + pulse * 2.5, 0, Math.PI * 2);
				ctx.fill();
				// Core dot
				ctx.fillStyle = '#d97706';
				ctx.beginPath();
				ctx.arc(dotX, dotY, 3, 0, Math.PI * 2);
				ctx.fill();
			} else if (isDone) {
				ctx.fillStyle = '#059669';
				ctx.beginPath();
				ctx.arc(dotX, dotY, 3.5, 0, Math.PI * 2);
				ctx.fill();
			} else {
				ctx.fillStyle = '#cbd5e1';
				ctx.beginPath();
				ctx.arc(dotX, dotY, 3, 0, Math.PI * 2);
				ctx.fill();
			}

			// Node Label
			ctx.font = '500 11px Inter, system-ui, sans-serif';
			ctx.textAlign = 'left';
			ctx.textBaseline = 'middle';
			ctx.fillStyle = isActive
				? '#92400e'
				: isDone
					? '#065f46'
					: '#64748b';
			ctx.fillText(node.name.replace(/_/g, ' '), node.x + 30, node.y + node.height / 2);

			// Attempt counter pill
			if ((node.attempts ?? 0) > 1) {
				const badge = `#${node.attempts}`;
				ctx.font = '600 9px SFMono-Regular, Consolas, monospace';
				ctx.fillStyle = '#64748b';
				ctx.textAlign = 'right';
				ctx.fillText(badge, node.x + node.width - 12, node.y + node.height / 2);
			}

			ctx.restore();
		}

		ctx.restore();
	}

	function startAnimLoop() {
		animFrameId = null;
		if (!inViewport || !documentVisible) return;
		const hasActive = graphModel.nodes.some(
			(n) => n.status === 'active' || attentionPhaseNames.includes(n.name)
		);
		if (hasActive) {
			redraw();
			animFrameId = requestAnimationFrame(startAnimLoop);
		} else {
			redraw();
		}
	}

	$effect(() => {
		if (!containerEl) return;
		const updateVisibility = () => { documentVisible = !document.hidden; };
		updateVisibility();
		document.addEventListener('visibilitychange', updateVisibility);
		const observer = typeof IntersectionObserver === 'undefined' ? null : new IntersectionObserver((entries) => {
			inViewport = entries.some((entry) => entry.isIntersecting);
		});
		inViewport = observer === null;
		observer?.observe(containerEl);
		return () => {
			observer?.disconnect();
			document.removeEventListener('visibilitychange', updateVisibility);
		};
	});

	$effect(() => {
		if (containerEl && !initialFitDone && graphModel.nodes.length) {
			initialFitDone = true;
			fitGraph();
		} else {
			redraw();
		}
	});

	$effect(() => {
		if (animFrameId !== null) cancelAnimationFrame(animFrameId);
		startAnimLoop();
		return () => {
			if (animFrameId !== null) cancelAnimationFrame(animFrameId);
			animFrameId = null;
		};
	});
</script>

<section
	class="overflow-hidden rounded-xl border border-surface-700 bg-surface-900 shadow-sm"
	data-testid="workflow-card"
	data-execution={execution?.execution_id ?? ''}
>
	<!-- Header Button -->
	<button
		type="button"
		class="flex w-full items-start gap-3 p-4 text-left transition hover:bg-surface-800/60"
		onclick={() => onEnterPhase(currentPhase, { readOnly: false })}
		data-testid="workflow-card-body"
		title="Enter the current phase orchestrator"
	>
		<div class="min-w-0 flex-1">
			<header class="flex flex-wrap items-center gap-2">
				<h4 class="truncate text-base font-semibold text-slate-800">{title}</h4>
				<span
					class="rounded-full border px-2.5 py-0.5 font-mono text-[11px] uppercase font-medium {TONE[status] ??
						'border-surface-600 text-slate-500'}"
					data-testid="workflow-card-status"
				>
					{status}
				</span>
				{#if execution}
					<span class="font-mono text-xs text-slate-500">
						phase {canonicalPhaseName(execution.phase ?? '—')} · {execution.execution_id}
					</span>
				{/if}
			</header>
			{#if summary}
				<p class="mt-1.5 text-xs text-slate-600 leading-relaxed" data-testid="workflow-card-summary">
					{summary}
				</p>
			{/if}
			{#if attentionPhaseNames.length}
				<p
					class="mt-2.5 inline-flex items-center gap-2 rounded-lg border border-warn/30 bg-warn/10 px-2.5 py-1 text-xs font-semibold text-warn"
					data-testid="workflow-card-needs-input"
				>
					<span class="h-2 w-2 animate-pulse rounded-full bg-warn" aria-hidden="true"></span>
					Input needed for {attentionPhaseNames.join(', ')} — click to answer.
				</p>
			{/if}
		</div>
		<span class="mt-1 text-sm text-slate-400 font-bold transition hover:text-slate-600" aria-hidden="true">›</span>
	</button>

	<!-- Directed Phase Graph Area with HTML5 Canvas Pan & Zoom -->
	{#if definitionError}
		<p class="px-4 pb-3 text-xs text-warn" role="status" data-testid="workflow-definition-error">{definitionError}</p>
	{/if}
	{#if graphModel.nodes.length}
		<div class="border-t border-surface-700 bg-surface-950/40">
			<!-- Graph Sub-header with Legend -->
			<div class="flex items-center justify-between border-b border-surface-700/60 px-4 py-2 text-[11px] text-slate-500 font-mono">
				<div class="flex items-center gap-2">
					<span class="h-1.5 w-1.5 rounded-full bg-slate-400" aria-hidden="true"></span>
					<span class="font-semibold uppercase tracking-wider text-slate-600">Phase Transition Graph</span>
				</div>

				<div class="flex items-center gap-3 text-[10px]">
					<span class="inline-flex items-center gap-1.5">
						<span class="h-2 w-2 rounded-full border border-profit/60 bg-profit/20"></span>
						done
					</span>
					<span class="inline-flex items-center gap-1.5">
						<span class="h-2 w-2 rounded-full border border-warn bg-warn/30"></span>
						active
					</span>
					<span class="inline-flex items-center gap-1.5 text-warn/80">
						<span class="h-0 w-3 border-t border-dashed border-warn"></span>
						loopback
					</span>
				</div>
			</div>

			<!-- Canvas Container with Pan & Zoom -->
			<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
			<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
			<div
				bind:this={containerEl}
				class="relative h-64 w-full overflow-hidden select-none cursor-grab active:cursor-grabbing focus:outline-none focus:ring-1 focus:ring-accent"
				data-testid="workflow-phase-stepper"
				data-testid-graph="workflow-phase-graph"
				role="region"
				tabindex="0"
				onwheel={handleWheel}
				onpointerdown={handlePointerDown}
				onpointermove={handlePointerMove}
				onpointerup={handlePointerUp}
				onclick={handleCanvasClick}
				onkeydown={handleKeyDown}
				aria-label="Workflow phase transition canvas"
			>
				<!-- HTML5 Canvas -->
				<canvas
					bind:this={canvasEl}
					class="absolute inset-0 h-full w-full pointer-events-none"
				></canvas>

				<!-- Synchronized Accessible HTML Buttons for Drill-In & Testability -->
				<div
					class="absolute inset-0 pointer-events-none"
					style="transform: translate({panX}px, {panY}px) scale({zoom}); transform-origin: 0 0;"
				>
					{#each graphModel.nodes as node (node.name)}
						{@const isAttention = attentionPhaseNames.includes(node.name)}
						{@const isDone = node.status === 'done'}
						{@const isPending = node.status === 'pending'}
						<button
							type="button"
							style="position: absolute; left: {node.x}px; top: {node.y}px; width: {node.width}px; height: {node.height}px;"
							class="pointer-events-auto rounded-lg focus:outline-none focus-visible:ring-2 focus-visible:ring-accent transition disabled:cursor-not-allowed opacity-0 hover:opacity-10 focus-visible:opacity-100 bg-surface-900/10"
							disabled={isPending}
							onclick={() => handleNodeClick(node.name, isDone)}
							onpointerdown={handlePointerDown}
							data-testid="phase-step"
							data-phase={node.name}
							data-status={node.status}
							data-attention={isAttention ? 'true' : 'false'}
							title={isAttention
								? `Input needed — enter the ${node.name} agent to answer`
								: isDone
									? `View finished ${node.name} orchestrator (read-only)`
									: node.status === 'active'
										? `Enter active ${node.name} orchestrator`
										: `${node.name} (pending)`}
						>
							<span class="sr-only">
								{#if node.status === 'active'}
									<span data-testid="phase-liveness">active</span>
								{/if}
								{node.name.replace(/_/g, ' ')}
							</span>
						</button>
					{/each}
				</div>

				<!-- Pan & Zoom Hint Indicator -->
				<div class="pointer-events-none absolute bottom-2 left-3 rounded bg-surface-900/70 px-2 py-0.5 font-mono text-[9px] text-slate-400 backdrop-blur-xs">
					Drag to pan · Scroll to zoom · Click node to drill in
				</div>
			</div>
		</div>
	{/if}
</section>
