<script lang="ts">
	import type { ToolResult } from '$lib/types';
	import { formatValue, isRecord, readNumber, readRecord, readString } from './safe';

	let { result }: { result: ToolResult } = $props();

	const data = $derived(isRecord(result.structured) ? result.structured : {});
	const boundary = $derived(readString(data, 'boundary'));
	const runId = $derived(readString(data, 'run_id'));
	const jobId = $derived(readString(data, 'job_id'));
	const state = $derived(readString(data, 'state'));
	const status = $derived(readString(data, 'status'));
	const location = $derived(readString(data, 'location'));
	const exitCode = $derived(readNumber(data, 'exit_code'));
	const metrics = $derived(readRecord(data, 'metrics'));
	const metricRows = $derived(Object.entries(metrics));

	const identifier = $derived(runId ?? jobId ?? status ?? state);
	const statusText = $derived(state ?? status);
</script>

<div class="space-y-2" data-testid="backtest-tool-card">
	<dl class="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 font-mono text-[11px]">
		{#if boundary}<dt class="text-slate-500">boundary</dt><dd class="text-slate-800">{boundary}</dd>{/if}
		{#if runId}<dt class="text-slate-500">run_id</dt><dd class="text-slate-800">{runId}</dd>{/if}
		{#if jobId}<dt class="text-slate-500">job_id</dt><dd class="text-slate-800">{jobId}</dd>{/if}
		{#if statusText}<dt class="text-slate-500">state</dt><dd class="text-slate-800">{statusText}</dd>{/if}
		{#if exitCode !== null}<dt class="text-slate-500">exit_code</dt><dd class="text-slate-800">{exitCode}</dd>{/if}
		{#if location}<dt class="text-slate-500">location</dt><dd class="text-slate-800">{location}</dd>{/if}
	</dl>

	{#if !boundary && !identifier && exitCode === null}
		<p class="text-xs text-slate-500">{result.summary || 'No structured details reported.'}</p>
	{/if}

	{#if metricRows.length}
		<div>
			<p class="mb-1 text-[10px] tracking-wider text-slate-500 uppercase">metrics</p>
			<div class="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-0.5 font-mono text-[11px]">
				{#each metricRows as [key, value] (key)}
					<span class="text-slate-500">{key}</span>
					<span class="text-slate-800">{formatValue(value)}</span>
				{/each}
			</div>
		</div>
	{/if}
</div>
