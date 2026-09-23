<script lang="ts">
	import { getSubAgentLiveView } from '../sub-agent-live-view';
	import type { ToolResult } from '$lib/types';
	import { isRecord, readString } from './safe';

	/**
	 * Renderer for the `sub_agent_handle` datum: one spawned sub-agent.
	 *
	 * The datum's `structured` carries the child handle (id, kind, role, and
	 * the delegated task); the card upgrades to the live node status and the
	 * drill-in navigation whenever the surrounding chat surface provides the
	 * sub-agent live view, so the same card renders in the root transcript
	 * and inside every node scope. Clicking it nests into the child's own
	 * chat surface, where its initial prompt is the first user message.
	 */
	let { result }: { result: ToolResult; workspaceId: string } = $props();

	const liveView = getSubAgentLiveView();

	const structured = $derived(isRecord(result.structured) ? result.structured : {});

	const subAgentId = $derived(
		readString(structured, 'subAgentId') ?? readString(structured, 'sub_agent_id') ?? ''
	);

	const node = $derived.by(() => {
		if (!subAgentId) return null;
		return liveView?.().nodeFor(subAgentId) ?? null;
	});

	const pending = $derived.by(() => {
		if (!subAgentId) return false;
		return liveView?.().pendingFor(subAgentId) ?? false;
	});

	const role = $derived(readString(structured, 'role') ?? node?.role ?? 'sub-agent');
	const task = $derived(readString(structured, 'task') ?? '');
	const kind = $derived(readString(structured, 'kind') ?? 'conversation');

	const status = $derived(
		pending
			? 'running'
			: (node?.status ?? readString(structured, 'status') ?? 'spawned')
	);

	const STATUS_TONE: Record<string, string> = {
		spawned: 'border-accent/40 bg-accent/10 text-accent',
		pending: 'border-accent/40 bg-accent/10 text-accent',
		running: 'border-accent/40 bg-accent/10 text-accent',
		progress: 'border-accent/40 bg-accent/10 text-accent',
		needs_input: 'border-warn/40 bg-warn/10 text-warn',
		completed: 'border-profit/40 bg-profit/10 text-profit',
		failed: 'border-loss/40 bg-loss/10 text-loss'
	};

	const initials = $derived(
		role
			.split(/[\s_-]+/)
			.filter(Boolean)
			.slice(0, 2)
			.map((word) => word[0]?.toUpperCase() ?? '')
			.join('') || 'SA'
	);

	function enter(): void {
		if (!subAgentId) return;
		liveView?.().onEnterSubAgent(subAgentId);
	}
</script>

<button
	type="button"
	class="group flex w-full items-start gap-3 rounded-xl border border-surface-700 bg-surface-900 p-3 text-left transition hover:border-accent/40 hover:bg-surface-800/50"
	onclick={enter}
	data-testid="sub-agent-card"
	data-sub-agent={subAgentId}
	data-status={status}
	data-pending={pending ? 'true' : 'false'}
	title={subAgentId ? `Open the ${role} sub-agent` : undefined}
>
	<span
		class="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-surface-700 bg-surface-800 font-mono text-[11px] uppercase text-slate-500 transition group-hover:border-accent/40 group-hover:text-accent"
		aria-hidden="true"
	>
		{#if pending}
			<span
				class="h-2 w-2 animate-pulse rounded-full bg-accent"
				data-testid="sub-agent-card-liveness"
			></span>
		{:else}
			{initials}
		{/if}
	</span>
	<span class="min-w-0 flex-1">
		<header class="flex flex-wrap items-center gap-2">
			<h4 class="truncate text-sm font-semibold text-slate-800">{role}</h4>
			<span
				class="rounded-full border px-2 py-0.5 font-mono text-[10px] uppercase {STATUS_TONE[status] ??
					'border-surface-600 text-slate-500'}"
				data-testid="sub-agent-card-status"
			>
				{status}
			</span>
			<span class="font-mono text-[10px] text-slate-500">{kind.replace(/_/g, ' ')}</span>
		</header>
		{#if task}
			<p class="mt-1 truncate text-xs text-slate-500" title={task} data-testid="sub-agent-card-task">
				{task}
			</p>
		{/if}
		<p class="mt-1 font-mono text-[10px] text-slate-500">
			{subAgentId || 'spawning'}
		</p>
		{#if status === 'needs_input'}
			<p
				class="mt-1 flex items-center gap-1.5 text-xs font-semibold text-warn"
				data-testid="sub-agent-card-needs-input"
			>
				<span class="h-1.5 w-1.5 animate-pulse rounded-full bg-warn" aria-hidden="true"></span>
				Input needed — open the agent to answer.
			</p>
		{/if}
	</span>
	<span class="mt-1 text-xs text-slate-500 transition group-hover:text-accent" aria-hidden="true"
		>›</span
	>
</button>
