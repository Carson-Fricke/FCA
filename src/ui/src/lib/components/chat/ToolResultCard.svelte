<script lang="ts">
	import type { ToolResult } from '$lib/types';
	import { isStandaloneCard, toolResultComponent } from './tool-results/registry';
	import { isRecord, prettyJson } from './tool-results/safe';

	let { result, workspaceId }: { result: ToolResult; workspaceId: string } = $props();

	const Card = $derived(toolResultComponent(result.pending || result.outcome || !result.ok ? '' : result.tool ?? '', result.pending || result.outcome || !result.ok ? null : result.datumType));
	// Standalone cards retain their own chrome, but share execution metadata.
	const standalone = $derived(result.ok && !result.pending && !result.outcome && isStandaloneCard(result.datumType));
	const structured = $derived(isRecord(result.structured) ? result.structured : {});
	const startedAt = $derived(result.startedAt ?? structured.startedAt);
	const durationMs = $derived(result.durationMs ?? structured.durationMs);
	const reused = $derived(structured.reused);
	const argumentsData = $derived(result.arguments ?? structured.arguments);
</script>

	<div
		class={standalone ? 'space-y-2' : 'rounded-lg border border-surface-700 bg-surface-900/60 p-3'}
		data-testid="tool-result-card"
		data-tool={result.tool}
		data-datum-type={result.datumType}
	>
		{#if !standalone}
		<header class="mb-2 flex items-center gap-2">
			<span class="font-mono text-[11px] text-slate-600">{result.tool ?? result.datumType ?? 'Tool result'}</span>
			{#if result.datumType && result.tool && result.datumType !== result.tool}
				<span class="font-mono text-[10px] text-slate-500">{result.datumType}</span>
			{/if}
			<span
				class="rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase {result.pending ? 'bg-accent/15 text-accent' : result.ok
					? 'bg-profit/15 text-profit'
					: 'bg-loss/15 text-loss'}"
				data-testid="tool-result-status"
			>
				{result.pending ? 'pending' : result.outcome ?? (result.ok ? 'ok' : 'failed')}
			</span>
			{#if result.callId}
				<span class="ml-auto font-mono text-[10px] text-slate-500">{result.callId}</span>
			{/if}
		</header>
		{/if}
		<Card {result} {workspaceId} />
		{#if startedAt != null || durationMs != null || reused != null || argumentsData != null}
			<div class="mt-2 space-y-1" data-testid="tool-metadata">
				{#if startedAt != null}
					<p class="font-mono text-[11px] text-slate-500" data-testid="tool-started-at">started: {String(startedAt)}</p>
				{/if}
				{#if durationMs != null}
					<p class="font-mono text-[11px] text-slate-500">duration: {String(durationMs)} ms</p>
				{/if}
				{#if reused != null}
					<p class="font-mono text-[11px] text-slate-500">reused: {String(reused)}</p>
				{/if}
				{#if argumentsData != null}
					<details>
						<summary class="cursor-pointer text-[11px] text-slate-500">arguments</summary>
						<pre class="mt-1 max-h-48 overflow-auto font-mono text-[11px] whitespace-pre-wrap text-slate-600">{prettyJson(argumentsData)}</pre>
					</details>
				{/if}
			</div>
		{/if}
	</div>
