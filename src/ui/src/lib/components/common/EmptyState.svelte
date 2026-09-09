<script lang="ts">
	/** Explicit "quiet empty" state per the adopted visual language. */
	let {
		title,
		hint = ''
	}: {
		title: string;
		hint?: string;
	} = $props();
</script>

<div class="rounded-xl border border-white/5 bg-surface-900/40 px-6 py-10 text-center">
	<div class="mx-auto mb-3 h-8 w-8 rounded-full border border-accent/30 bg-accent/10"></div>
	<p class="text-sm font-medium text-slate-300">{title}</p>
	{#if hint}
		<p class="mt-1 text-xs text-slate-500">{hint}</p>
	{/if}
</div>
