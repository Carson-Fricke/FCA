<script lang="ts">
	import type { ToolResult } from '$lib/types';
	import { isRecord, prettyJson } from './safe';

	let { result, hideSummary=false }: { result: ToolResult; workspaceId: string; hideSummary?:boolean } = $props();

	const structured = $derived(isRecord(result.structured) ? result.structured : {});
	const structuredJson = $derived(prettyJson(structured));
	const hasStructured = $derived(Object.keys(structured).length > 0);
</script>

<div class="space-y-2" data-testid="generic-tool-card">
	{#if result.summary && !hideSummary}
		<p class="text-xs text-slate-700">{result.summary}</p>
	{/if}

	{#if result.failure}
		<p
			class="rounded border border-loss/30 bg-loss/10 px-2 py-1 text-xs text-loss"
			data-testid="tool-failure"
		>
			{String(result.failure.kind ?? 'Tool failure')}: {String(result.failure.message ?? result.summary ?? 'No failure description supplied')}
		</p>
	{/if}

	{#if result.text}
		<pre
			class="max-h-48 overflow-auto rounded bg-surface-800 p-2 font-mono text-[11px] whitespace-pre-wrap text-slate-600">{result.text}</pre>
	{/if}

	{#if hasStructured}
		<details>
			<summary class="cursor-pointer text-[11px] text-slate-500">structured</summary>
			<pre
				class="mt-1 max-h-48 overflow-auto rounded bg-surface-800 p-2 font-mono text-[11px] text-slate-600">{structuredJson}</pre>
		</details>
	{/if}
</div>
