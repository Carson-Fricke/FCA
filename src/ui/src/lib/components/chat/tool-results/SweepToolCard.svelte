<script lang="ts">
	import type { ToolResult } from '$lib/types';
	import { isRecord, readArray, readNumber, readString } from './safe';

	let { result }: { result: ToolResult } = $props();

	const data = $derived(isRecord(result.structured) ? result.structured : {});
	const sweepId = $derived(readString(data, 'sweep_id'));
	const requested = $derived(readNumber(data, 'requested'));
	const expanded = $derived(readNumber(data, 'expanded'));
	const duplicatesSkipped = $derived(readNumber(data, 'duplicates_skipped'));
	const runIds = $derived(readArray<string>(data, 'run_ids'));
</script>

<div class="space-y-2" data-testid="sweep-tool-card">
	<dl class="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 font-mono text-[11px]">
		{#if sweepId}<dt class="text-slate-500">sweep_id</dt><dd class="text-slate-800">{sweepId}</dd>{/if}
		{#if requested !== null}<dt class="text-slate-500">requested</dt><dd class="text-slate-800">{requested}</dd>{/if}
		{#if expanded !== null}<dt class="text-slate-500">expanded</dt><dd class="text-slate-800">{expanded}</dd>{/if}
		{#if duplicatesSkipped !== null}<dt class="text-slate-500">duplicates skipped</dt><dd class="text-slate-800">{duplicatesSkipped}</dd>{/if}
	</dl>

	{#if runIds.length}
		<div>
			<p class="mb-1 text-[10px] tracking-wider text-slate-500 uppercase">run ids</p>
			<ul class="space-y-0.5 font-mono text-[11px] text-slate-800">
				{#each runIds as runId, index (runId ?? index)}
					<li>{runId}</li>
				{/each}
			</ul>
		</div>
	{:else}
		<p class="text-xs text-slate-500">{result.summary || 'No runs reported.'}</p>
	{/if}
</div>
