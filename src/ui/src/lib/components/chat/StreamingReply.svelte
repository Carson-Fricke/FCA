<script lang="ts">
	import ThinkingAccordion from './ThinkingAccordion.svelte';
	import TypingDots from '$lib/components/common/TypingDots.svelte';

	/**
	 * Inline streaming assistant message. It uses the transcript's assistant
	 * styling (no card outline) and shows a typing indicator until the first
	 * token arrives, then the accumulating reply text. A live reasoning trace
	 * renders as a collapsed "Thinking" disclosure above the answer.
	 */
	let {
		active,
		text,
		reasoning = '',
		testId = 'streaming-reply'
	}: { active: boolean; text: string; reasoning?: string; testId?: string } = $props();
</script>

{#if active}
	<article class="flex justify-start" data-testid={testId} data-role="assistant">
		<div class="w-full p-1">
			<header class="mb-1 flex items-center gap-2 text-[10px] tracking-wider">
				<span class="text-slate-500 uppercase">assistant</span>
				<span class="font-mono text-slate-500">thesis-agent</span>
			</header>
			{#if reasoning}
				<ThinkingAccordion text={reasoning} testId="{testId}-thinking" />
			{/if}
			{#if text}
				<p class="text-sm whitespace-pre-wrap text-slate-800">{text}</p>
			{:else}
				<p class="py-0.5"><TypingDots /></p>
			{/if}
		</div>
	</article>
{/if}
