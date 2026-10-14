<script lang="ts">
	import MarkdownBody from './MarkdownBody.svelte';

	/**
	 * Collapsed-by-default disclosure for a model's reasoning trace. Deliberately
	 * subtle: no card outline, muted italic text, and a chevron that rotates on
	 * toggle. The answer text stays the primary content; thoughts are opt-in.
	 */
	let {
		text,
		testId = 'thinking-accordion',
		initiallyOpen = false,
		outputState = 'completed'
	}: { text: string; testId?: string; initiallyOpen?: boolean; outputState?: 'streaming' | 'interrupted' | 'completed' } = $props();

	// The initial open state is intentionally captured once per mounted accordion.
	// svelte-ignore state_referenced_locally
	let open = $state(initiallyOpen);
</script>

{#if text || outputState !== 'completed'}
	<div class="mb-1" data-testid={testId}>
		<button
			type="button"
			class="flex items-center gap-1 text-xs text-slate-500 italic transition-colors hover:text-slate-700"
			aria-expanded={open}
			data-testid="{testId}-toggle"
			onclick={() => (open = !open)}
		>
			<svg
				class="h-3 w-3 transition-transform duration-150 {open ? 'rotate-90' : ''}"
				viewBox="0 0 12 12"
				fill="none"
				stroke="currentColor"
				stroke-width="1.5"
				stroke-linecap="round"
				stroke-linejoin="round"
				aria-hidden="true"
			>
				<path d="M4.5 2.5 8 6l-3.5 3.5" />
			</svg>
			<span>{outputState === 'streaming' ? 'Thinking…' : outputState === 'interrupted' ? 'Thinking interrupted' : 'Thinking'}</span>
		</button>
		{#if open}
			<div class="mt-1 pl-4" data-testid="{testId}-body">
				<MarkdownBody text={text} class="text-xs text-slate-500 italic" />
			</div>
		{/if}
	</div>
{/if}
