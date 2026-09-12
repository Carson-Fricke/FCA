<script lang="ts">
	import type { WorkflowExecutionRecord, WorkflowPhase } from '$lib/types';

	/**
	 * Transcript item of kind `workflow`.
	 *
	 * A compact card: status chip, phase stepper, and a live summary. Clicking
	 * the card body enters the current phase orchestrator in-pane; the
	 * `↗ Open full page` link opens the standalone workflow route. Clicking a
	 * completed phase in the stepper enters that finished orchestrator
	 * read-only.
	 */
	let {
		execution = null,
		phases = [],
		status = 'running',
		summary = '',
		fullPageHref = null,
		onEnterPhase = () => {},
		onOpenFullPage
	}: {
		execution?: WorkflowExecutionRecord | null;
		phases?: WorkflowPhase[];
		status?: string;
		summary?: string;
		fullPageHref?: string | null;
		onEnterPhase?: (phase: string, options: { readOnly: boolean }) => void;
		onOpenFullPage?: () => void;
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
		phases.find((phase) => phase.status === 'active')?.name ??
			execution?.phase ??
			[...phases].reverse().find((phase) => phase.status === 'done')?.name ??
			'orchestrator'
	);

	const title = $derived(execution?.workflow_id ?? 'Workflow execution');
</script>

<section
	class="overflow-hidden rounded-xl border border-surface-700 bg-surface-900"
	data-testid="workflow-card"
	data-execution={execution?.execution_id ?? ''}
>
	<button
		type="button"
		class="flex w-full items-start gap-3 p-3 text-left transition hover:bg-surface-800/50"
		onclick={() => onEnterPhase(currentPhase, { readOnly: false })}
		data-testid="workflow-card-body"
		title="Enter the current phase orchestrator"
	>
		<div class="min-w-0 flex-1">
			<header class="flex flex-wrap items-center gap-2">
				<h4 class="truncate text-sm font-semibold text-slate-800">{title}</h4>
				<span
					class="rounded-full border px-2 py-0.5 font-mono text-[10px] uppercase {TONE[status] ??
						'border-surface-600 text-slate-500'}"
					data-testid="workflow-card-status"
				>
					{status}
				</span>
				{#if execution}
					<span class="font-mono text-[10px] text-slate-500">
						phase {execution.phase ?? '—'} · {execution.execution_id}
					</span>
				{/if}
			</header>
			{#if summary}
				<p class="mt-1 text-xs text-slate-500" data-testid="workflow-card-summary">{summary}</p>
			{/if}
		</div>
		<span class="mt-0.5 text-xs text-slate-500" aria-hidden="true">›</span>
	</button>

	{#if phases.length}
		<ol
			class="flex flex-wrap items-center gap-1.5 border-t border-surface-700 px-3 py-2"
			data-testid="workflow-phase-stepper"
			aria-label="Workflow phases"
		>
			{#each phases as phase, index (phase.name)}
				<li class="flex items-center gap-1.5">
					{#if index > 0}
						<span class="text-slate-600" aria-hidden="true">→</span>
					{/if}
					<button
						type="button"
						class="rounded-full border px-2 py-0.5 font-mono text-[10px] transition disabled:cursor-not-allowed disabled:opacity-40
							{phase.status === 'active'
							? 'border-accent/50 bg-accent/10 text-accent'
							: phase.status === 'done'
								? 'border-surface-600 text-slate-500 hover:border-accent/40 hover:text-accent'
								: 'border-surface-700 text-slate-500'}"
						disabled={phase.status === 'pending'}
						onclick={() => onEnterPhase(phase.name, { readOnly: phase.status === 'done' })}
						data-testid="phase-step"
						data-phase={phase.name}
						data-status={phase.status}
						title={phase.status === 'done'
							? `View finished ${phase.name} orchestrator (read-only)`
							: `Enter ${phase.name} orchestrator`}
					>
						{phase.name}
					</button>
				</li>
			{/each}
		</ol>
	{/if}

	{#if fullPageHref || onOpenFullPage}
		<div class="border-t border-surface-700 px-3 py-1.5">
			{#if fullPageHref}
				<a
					href={fullPageHref}
					class="text-[11px] text-accent hover:underline"
					data-testid="workflow-open-full"
					onclick={() => onOpenFullPage?.()}
				>
					↗ Open full page
				</a>
			{:else}
				<button
					type="button"
					class="text-[11px] text-accent hover:underline"
					data-testid="workflow-open-full"
					onclick={() => onOpenFullPage?.()}
				>
					↗ Open full page
				</button>
			{/if}
		</div>
	{/if}
</section>
