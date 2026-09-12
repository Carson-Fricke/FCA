<script lang="ts">
	import type { ToolResult } from '$lib/types';
	import { isRecord, readString } from './safe';

	let { result }: { result: ToolResult } = $props();

	const data = $derived(isRecord(result.structured) ? result.structured : {});
	const jobId = $derived(readString(data, 'job_id'));
	const state = $derived(readString(data, 'state'));
	const errorMessage = $derived(readString(data, 'error_message'));
	const createdAt = $derived(readString(data, 'created_at'));
	const updatedAt = $derived(readString(data, 'updated_at'));
</script>

<div class="space-y-2" data-testid="job-tool-card">
	<dl class="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 font-mono text-[11px]">
		{#if jobId}<dt class="text-slate-500">job_id</dt><dd class="text-slate-800">{jobId}</dd>{/if}
		{#if state}<dt class="text-slate-500">state</dt><dd class="text-slate-800">{state}</dd>{/if}
		{#if createdAt}<dt class="text-slate-500">created_at</dt><dd class="text-slate-800">{createdAt}</dd>{/if}
		{#if updatedAt}<dt class="text-slate-500">updated_at</dt><dd class="text-slate-800">{updatedAt}</dd>{/if}
	</dl>

	{#if errorMessage}
		<p
			class="rounded border border-loss/30 bg-loss/10 px-2 py-1 text-xs text-loss"
			data-testid="job-error"
		>
			{errorMessage}
		</p>
	{/if}

	{#if !jobId && !state}
		<p class="text-xs text-slate-500">{result.summary || 'No job details reported.'}</p>
	{/if}
</div>