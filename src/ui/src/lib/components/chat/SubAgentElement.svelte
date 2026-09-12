<script lang="ts">
	import ToolResultCard from './ToolResultCard.svelte';
	import ThinkingAccordion from './ThinkingAccordion.svelte';
	import GateCard from './GateCard.svelte';
	import SubAgentComposer from './SubAgentComposer.svelte';
	import Self from './SubAgentElement.svelte';
	import {
		deriveGates,
		parseReasoning,
		parseToolResult,
		type SubAgentTreeNode
	} from '$lib/subagent';
	import type { ExecutionEvent, GateResolution, SubAgentNode, WorkflowGate } from '$lib/types';

	/**
	 * Recursive transcript element for item kind `sub_agent`.
	 *
	 * Header: role · phase · status · `fresh context` badge · `cutoff` badge ·
	 * `MCP: n tools`. Body: the node's scoped transcript (thinking, tool-result
	 * cards, nested `SubAgentElement`s that are themselves clickable to enter),
	 * and a composer only when the node `accepts_input`.
	 */
	let {
		node,
		events = [],
		children = [],
		workspaceId = '',
		expanded,
		onToggle,
		isExpanded,
		onEnter = () => {},
		onSubmit,
		onResolveGate,
		activeId = null,
		readOnly = false,
		busy = false,
		depth = 0
	}: {
		node: SubAgentNode;
		events?: ExecutionEvent[];
		children?: SubAgentTreeNode[];
		workspaceId?: string;
		expanded?: boolean;
		onToggle?: (subAgentId: string) => void;
		isExpanded?: (subAgentId: string) => boolean;
		onEnter?: (node: SubAgentNode) => void;
		onSubmit?: (node: SubAgentNode, body: string) => void;
		onResolveGate?: (gate: WorkflowGate, resolution: GateResolution) => void;
		activeId?: string | null;
		readOnly?: boolean;
		busy?: boolean;
		depth?: number;
	} = $props();

	const STATUS_TONE: Record<string, string> = {
		spawned: 'border-accent/40 bg-accent/10 text-accent',
		running: 'border-accent/40 bg-accent/10 text-accent',
		progress: 'border-accent/40 bg-accent/10 text-accent',
		needs_input: 'border-warn/40 bg-warn/10 text-warn',
		completed: 'border-profit/40 bg-profit/10 text-profit',
		failed: 'border-loss/40 bg-loss/10 text-loss'
	};

	let internalOpen = $state(true);
	const isOpen = $derived(expanded ?? internalOpen);
	const isActive = $derived(activeId === node.subAgentId);

	const toolResults = $derived(
		events
			.map((event) => parseToolResult(event))
			.filter((result): result is NonNullable<typeof result> => result !== null)
	);
	const gates = $derived(deriveGates(events));
	const thinking = $derived(
		events
			.map((event) => parseReasoning(event))
			.filter((value): value is string => value !== null)
			.join('\n\n')
	);

	function toggle(): void {
		if (onToggle) onToggle(node.subAgentId);
		else internalOpen = !internalOpen;
	}

	function enter(): void {
		onEnter(node);
	}

	function acceptsInput(): boolean {
		return node.acceptsInput && !readOnly;
	}
</script>

<article
	class="rounded-xl border {isActive ? 'border-accent/50' : 'border-surface-700'} bg-surface-900"
	style="margin-left: {depth ? 12 : 0}px"
	data-testid="sub-agent-element"
	data-sub-agent={node.subAgentId}
	data-depth={depth}
>
	<header class="flex items-start gap-2 p-2.5">
		<button
			type="button"
			class="min-w-0 flex-1 text-left"
			onclick={enter}
			data-testid="sub-agent-enter"
			title="Enter this sub-agent"
		>
			<div class="flex flex-wrap items-center gap-2">
				<span class="truncate text-sm font-semibold text-slate-800" data-testid="sub-agent-role">
					{node.role || node.subAgentId}
				</span>
				{#if node.phase}
					<span class="font-mono text-[10px] text-slate-500" data-testid="sub-agent-phase">
						{node.phase}
					</span>
				{/if}
				<span
					class="rounded-full border px-2 py-0.5 font-mono text-[10px] uppercase {STATUS_TONE[node.status] ??
						'border-surface-600 text-slate-500'}"
					data-testid="sub-agent-status"
				>
					{node.status}
				</span>
				{#if node.context.isolated}
					<span
						class="rounded-full border border-accent/40 px-2 py-0.5 font-mono text-[10px] text-accent"
						data-testid="sub-agent-fresh-context"
					>
						fresh context
					</span>
				{/if}
				{#if node.context.knowledge_cutoff}
					<span
						class="rounded-full border border-warn/40 px-2 py-0.5 font-mono text-[10px] text-warn"
						data-testid="sub-agent-cutoff"
					>
						cutoff {node.context.knowledge_cutoff}
					</span>
				{/if}
				{#if node.context.mcp.enabled}
					<span
						class="rounded-full border border-surface-600 px-2 py-0.5 font-mono text-[10px] text-slate-500"
						data-testid="sub-agent-mcp"
					>
						MCP: {node.context.mcp.tools.length} tools
					</span>
				{/if}
			</div>
			<p class="mt-0.5 font-mono text-[10px] text-slate-500">{node.subAgentId}</p>
		</button>
		<button
			type="button"
			class="rounded p-1 text-slate-500 transition hover:text-accent"
			onclick={toggle}
			aria-expanded={isOpen}
			aria-label={isOpen ? 'Collapse sub-agent' : 'Expand sub-agent'}
			data-testid="sub-agent-toggle"
		>
			<span class="inline-block transition-transform {isOpen ? 'rotate-90' : ''}" aria-hidden="true">›</span>
		</button>
	</header>

	{#if isOpen}
		<div class="border-t border-surface-700 p-2.5" data-testid="sub-agent-transcript">
			{#if thinking}
				<ThinkingAccordion text={thinking} testId="sub-agent-{node.subAgentId}-thinking" />
			{/if}

			{#each toolResults as result, index (result.callId ?? `${result.tool}-${index}`)}
				<div class="mb-2">
					<ToolResultCard {result} {workspaceId} />
				</div>
			{/each}

			{#each gates as gate (gate.gateId)}
				<div class="mb-2">
					<GateCard {gate} {busy} onResolve={(resolution) => onResolveGate?.(gate, resolution)} />
				</div>
			{/each}

			{#if children.length}
				<div class="space-y-1.5" data-testid="sub-agent-children">
					{#each children as child (child.node.subAgentId)}
						<Self
							node={child.node}
							events={child.events}
							children={child.children}
							{workspaceId}
							expanded={isExpanded?.(child.node.subAgentId)}
							{onToggle}
							{isExpanded}
							{onEnter}
							{onSubmit}
							{onResolveGate}
							{activeId}
							{readOnly}
							{busy}
							depth={depth + 1}
						/>
					{/each}
				</div>
			{/if}

			{#if !thinking && !toolResults.length && !gates.length && !children.length}
				<p class="text-xs text-slate-500">No scoped events yet.</p>
			{/if}
		</div>

		{#if acceptsInput()}
			<SubAgentComposer
				acceptsInput={acceptsInput()}
				{busy}
				onSubmit={(body) => onSubmit?.(node, body)}
			/>
		{/if}
	{/if}
</article>
