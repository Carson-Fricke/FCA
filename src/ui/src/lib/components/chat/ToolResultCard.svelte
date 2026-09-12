<script lang="ts">
	import type { ToolResult } from '$lib/types';
	import { toolResultComponent } from './tool-results/registry';

	let { result, workspaceId }: { result: ToolResult; workspaceId: string } = $props();

	const Card = $derived(toolResultComponent(result.tool));
</script>

<div
	class="rounded-lg border border-surface-700 bg-surface-900/60 p-3"
	data-testid="tool-result-card"
	data-tool={result.tool}
>
	<header class="mb-2 flex items-center gap-2">
		<span class="font-mono text-[11px] text-slate-600">{result.tool}</span>
		<span
			class="rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase {result.ok
				? 'bg-profit/15 text-profit'
				: 'bg-loss/15 text-loss'}"
			data-testid="tool-result-status"
		>
			{result.ok ? 'ok' : 'failed'}
		</span>
		{#if result.callId}
			<span class="ml-auto font-mono text-[10px] text-slate-500">{result.callId}</span>
		{/if}
	</header>

	<Card {result} {workspaceId} />
</div>