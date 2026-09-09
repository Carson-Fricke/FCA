<script lang="ts">
	import type { CleoPlan, CleoTaskEvent } from '$lib/types';

	/** Read-only projection of the workspace's CLEO plan and tasks. */
	let {
		plan,
		timeline = []
	}: {
		plan: CleoPlan;
		timeline?: CleoTaskEvent[];
	} = $props();

	const STEP_TONE: Record<string, string> = {
		done: 'text-profit border-profit/40',
		active: 'text-accent border-accent/40',
		pending: 'text-slate-500 border-surface-600',
		blocked: 'text-loss border-loss/40'
	};

	const EVENT_TONE: Record<string, string> = {
		'task.completed': 'text-profit',
		'task.started': 'text-accent',
		'task.created': 'text-slate-400',
		'gate.passed': 'text-profit',
		'gate.blocked': 'text-warn'
	};
</script>

<section
	class="rounded-xl border border-white/5 bg-surface-900/60 p-3"
	data-testid="cleo-plan-panel"
	data-stage={plan.current_stage}
>
	<div class="mb-2 flex items-baseline gap-2">
		<h4 class="text-[11px] font-semibold uppercase tracking-wider text-slate-500">
			CLEO plan · read-only
		</h4>
		<span class="ml-auto font-mono text-[11px] text-slate-500">
			{plan.plan_id} · {plan.current_stage}
		</span>
	</div>
	<p class="mb-3 text-sm text-slate-200">{plan.title}</p>

	<ol class="space-y-1.5">
		{#each plan.steps as step (step.id)}
			<li class="flex items-start gap-2 text-xs">
				<span class="mt-0.5 h-3 w-3 shrink-0 rounded-full border {STEP_TONE[step.state]}"></span>
				<span class="text-slate-300">{step.title}</span>
			</li>
		{/each}
	</ol>

	{#if timeline.length}
		<h5 class="mt-3 mb-1 text-[11px] font-semibold uppercase tracking-wider text-slate-500">
			Recent activity
		</h5>
		<ol class="space-y-1">
			{#each timeline.slice(-5) as item (item.id)}
				<li class="flex items-center gap-2 font-mono text-[11px]">
					<span class="text-slate-600">{item.at.slice(11, 19)}</span>
					<span class="{EVENT_TONE[item.kind] ?? 'text-slate-400'}">{item.kind}</span>
					<span class="truncate text-slate-500">{item.detail}</span>
				</li>
			{/each}
		</ol>
	{/if}
</section>
