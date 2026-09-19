<script lang="ts">
	import type { ToolResult } from '$lib/types';
	import { isRecord, readString } from './safe';

	/**
	 * The orange operator-request box: an agent asked for input and is waiting
	 * for the operator's reply. Renders the request's `message` (question) and
	 * its `reason` from the tool result's structured payload, reusing the
	 * existing needs-input styling (no new visual language).
	 */
	let { result }: { result: ToolResult; workspaceId: string } = $props();

	const structured = $derived(isRecord(result.structured) ? result.structured : {});
	const question = $derived(readString(structured, 'question') ?? result.summary);
	const reason = $derived(readString(structured, 'reason'));
</script>

<div
	class="rounded-xl border border-warn/40 bg-warn/5 p-3"
	data-testid="operator-question-card"
>
	<header class="mb-1 flex items-center gap-2 text-[10px] tracking-wider">
		<span class="uppercase text-warn">question</span>
		<span
			class="rounded-full border border-warn/40 bg-warn/10 px-2 py-0.5 font-mono text-[10px] text-warn"
			data-testid="operator-question-waiting"
		>
			needs input
		</span>
	</header>
	<p class="text-sm whitespace-pre-wrap text-slate-800" data-testid="operator-question-message">
		{question}
	</p>
	{#if reason}
		<p class="mt-1 font-mono text-[10px] text-warn/80" data-testid="operator-question-reason">
			reason: {reason}
		</p>
	{/if}
</div>
