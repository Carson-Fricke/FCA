<script lang="ts">
	import type { Snippet } from 'svelte';

	/** Inline-confirm destructive action wrapper. */
	let {
		confirmLabel = 'Confirm remove',
		cancelLabel = 'Cancel',
		onConfirm,
		children
	}: {
		confirmLabel?: string;
		cancelLabel?: string;
		onConfirm: () => void;
		children?: Snippet;
	} = $props();

	let armed = $state(false);

	function arm(): void {
		armed = true;
	}

	function confirm(): void {
		armed = false;
		onConfirm();
	}
</script>

<span class="inline-flex items-center gap-1">
	{#if armed}
		<button
			type="button"
			class="rounded border border-loss/50 bg-loss/15 px-1.5 py-0.5 text-[10px] font-semibold text-loss hover:bg-loss/25"
			onclick={confirm}
			data-testid="inline-confirm-yes"
		>
			{confirmLabel}
		</button>
		<button
			type="button"
			class="rounded border border-surface-600 px-1.5 py-0.5 text-[10px] text-slate-400 hover:text-slate-200"
			onclick={() => (armed = false)}
			data-testid="inline-confirm-no"
		>
			{cancelLabel}
		</button>
	{:else}
		<button
			type="button"
			class="rounded px-1.5 py-0.5 text-[10px] text-slate-500 hover:text-slate-200"
			onclick={arm}
			data-testid="inline-confirm-arm"
		>
			{@render children?.()}
		</button>
	{/if}
</span>
