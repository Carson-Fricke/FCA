<script lang="ts">
	/** Inline streaming assistant bubble shown while a reply is generating. */
	let {
		active,
		text,
		testId = 'streaming-reply'
	}: { active: boolean; text: string; testId?: string } = $props();
</script>

{#if active}
	<div class="mt-3 rounded-lg border border-accent/30 bg-surface-900 p-3" data-testid={testId}>
		<p class="mb-1 text-[10px] font-semibold text-accent">thesis-agent</p>
		<p class="whitespace-pre-wrap text-sm text-slate-700">
			{text}<span class="animate-pulse text-accent">▍</span>
		</p>
	</div>
{/if}
