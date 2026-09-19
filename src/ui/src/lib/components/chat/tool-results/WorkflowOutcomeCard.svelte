<script lang="ts">
	import type { ToolResult } from '$lib/types';
	import { isRecord, readString } from './safe';

	/**
	 * The compact workflow-return card: what execution ended, in what status,
	 * and in which phase. It is deliberately not the live workflow card — the
	 * launch handle owns the phase stepper; this reports the terminal outcome.
	 */
	let { result }: { result: ToolResult; workspaceId: string } = $props();

	const structured = $derived(isRecord(result.structured) ? result.structured : {});
	const executionId = $derived(readString(structured, 'executionId') ?? 'unknown');
	const status = $derived(readString(structured, 'status') ?? (result.ok ? 'completed' : 'failed'));
	const phase = $derived(readString(structured, 'phase'));
	const detail = $derived(result.summary || result.text);

	const tone = $derived(
		status === 'completed'
			? 'border-profit/40 bg-profit/5 text-profit'
			: status === 'failed'
				? 'border-loss/40 bg-loss/5 text-loss'
				: 'border-warn/40 bg-warn/5 text-warn'
	);
</script>

<div class="rounded-xl border p-3 {tone}" data-testid="workflow-outcome-card">
	<header class="mb-1 flex flex-wrap items-center gap-2 text-[10px] tracking-wider">
		<span class="uppercase">workflow</span>
		<span
			class="rounded-full border border-current/40 px-2 py-0.5 font-mono text-[10px]"
			data-testid="workflow-outcome-status"
		>
			{status}
		</span>
		{#if phase}
			<span class="font-mono text-[10px]" data-testid="workflow-outcome-phase">
				phase: {phase}
			</span>
		{/if}
	</header>
	<p class="font-mono text-[11px] text-slate-500" data-testid="workflow-outcome-execution">
		{executionId}
	</p>
	{#if detail}
		<p class="mt-1 text-xs whitespace-pre-wrap text-slate-800">{detail}</p>
	{/if}
</div>