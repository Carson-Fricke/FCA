<script lang="ts">
	import type { Snippet } from 'svelte';

	/** Modal dialog shell. */
	let {
		title,
		onClose,
		children
	}: {
		title: string;
		onClose: () => void;
		children: Snippet;
	} = $props();
</script>

<div class="fixed inset-0 z-50 flex items-start justify-center bg-black/60 pt-24">
	<button type="button" class="absolute inset-0 cursor-default" aria-label="Close dialog" onclick={onClose}>
	</button>
	<div
		class="relative z-10 w-full max-w-lg rounded-2xl border border-surface-600 bg-surface-850 shadow-2xl"
		role="dialog"
		aria-label={title}
	>
		<div class="flex items-center justify-between border-b border-surface-700 px-4 py-3">
			<h2 class="text-sm font-semibold text-slate-800">{title}</h2>
			<button type="button" class="text-slate-500 hover:text-slate-800" onclick={onClose}>✕</button>
		</div>
		{@render children()}
	</div>
</div>
