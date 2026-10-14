<script lang="ts">
    import HarnessExecutionCard from "../HarnessExecutionCard.svelte";
	import type { ToolResult } from '$lib/types';
	import { formatValue, isRecord, readNumber, readRecord, readString } from './safe';

	let { result }: { result: ToolResult; workspaceId: string } = $props();

	const data = $derived(isRecord(result.structured) ? result.structured : {});
	const jobId = $derived(readString(data, 'job_id'));
	const state = $derived(readString(data, 'state'));
	const strategyId = $derived(readString(data, 'strategy_id'));
	const artifactCount = $derived(readNumber(data, 'artifact_count'));
	const totalBytes = $derived(readNumber(data, 'total_bytes'));
	const metrics = $derived(readRecord(data, 'metrics'));
	const metricRows = $derived(Object.entries(metrics));
</script>

<div class="space-y-2" data-testid="run-tool-card">
	<dl class="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 font-mono text-[11px]">
		{#if jobId}<dt class="text-slate-500">job_id</dt><dd class="text-slate-800">{jobId}</dd>{/if}
		{#if state}<dt class="text-slate-500">state</dt><dd class="text-slate-800">{state}</dd>{/if}
		{#if strategyId}<dt class="text-slate-500">strategy_id</dt><dd class="text-slate-800">{strategyId}</dd>{/if}
		{#if artifactCount !== null}<dt class="text-slate-500">artifacts</dt><dd class="text-slate-800">{artifactCount}</dd>{/if}
		{#if totalBytes !== null}<dt class="text-slate-500">total bytes</dt><dd class="text-slate-800">{totalBytes}</dd>{/if}
	</dl>

	{#if metricRows.length}
		<div>
			<p class="mb-1 text-[10px] tracking-wider text-slate-500 uppercase">metrics</p>
			<dl class="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-0.5 font-mono text-[11px]">
				{#each metricRows as [key, value] (key)}
					<dt class="text-slate-500">{key}</dt>
					<dd class="text-slate-800">{formatValue(value)}</dd>
				{/each}
			</dl>
		</div>
	{/if}
</div>

{#if jobId}<HarnessExecutionCard {jobId} />{/if}
