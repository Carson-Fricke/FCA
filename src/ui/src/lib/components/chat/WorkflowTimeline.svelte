<script lang="ts">
	import type { ExecutionEvent } from '$lib/types';

	/** Workflow timeline rendered from the durable execution event stream. */
	let { events = [] }: { events: ExecutionEvent[] } = $props();

	const TONE: Record<string, string> = {
		'workflow.launched': 'border-accent/50 text-accent',
		'workflow.started': 'border-accent/50 text-accent',
		'workflow.completed': 'border-profit/50 text-profit',
		'workflow.failed': 'border-loss/50 text-loss',
		'workflow.steered': 'border-warn/50 text-warn',
		'workflow.stopped': 'border-loss/50 text-loss',
		'workflow.cancelled': 'border-loss/50 text-loss',
		'workflow.paused': 'border-warn/50 text-warn'
	};

	const label = (type: string) => type.replace('workflow.', '').replace('tool.', 'tool ').replace('message.', 'message ');

	const summary = $derived.by(() => {
		if (!events.length) return '';
		const steered = events.filter((e) => e.eventType === 'workflow.steered').length;
		const last = events.at(-1);
		return steered ? `${steered} steering ack(s) · last cursor ${last?.cursor}` : `last cursor ${last?.cursor}`;
	});
</script>

<section
	class="rounded-xl border border-white/5 bg-surface-900/60 p-3"
	data-testid="workflow-timeline"
	data-cursor={events.at(-1)?.cursor ?? 0}
>
	<div class="mb-2 flex items-baseline gap-2">
		<h4 class="text-[10px] font-semibold uppercase tracking-wider text-slate-500">
			Execution timeline
		</h4>
		{#if summary}
			<span class="font-mono text-[10px] text-slate-600">{summary} · SSE resume at cursor {events.at(-1)?.cursor}</span>
		{/if}
	</div>
	{#if events.length === 0}
		<p class="text-xs text-slate-500">No durable events yet.</p>
	{:else}
		<ol class="space-y-1.5">
			{#each events as event (event.eventId)}
				<li class="flex items-center gap-2 font-mono text-xs">
					<span
						class="h-1.5 w-1.5 shrink-0 rounded-full border {TONE[event.eventType] ?? 'border-surface-600 text-slate-400'}"
						class:bg-accent={event.eventType.startsWith('workflow.')}
						aria-hidden="true"
					></span>
					<span class="w-32 shrink-0 text-slate-600">{event.occurredAt.slice(11, 19)}</span>
					<span class="{TONE[event.eventType] ?? 'text-slate-300'}">
						{label(event.eventType)}
					</span>
					{#if event.intentEpoch > 0}
						<span class="text-warn/80">epoch {event.intentEpoch}</span>
					{/if}
					{#if event.lateResult}
						<span class="text-slate-600">late result attached</span>
					{/if}
					<span class="truncate text-slate-500">{JSON.stringify(event.payload)}</span>
				</li>
			{/each}
		</ol>
	{/if}
</section>
