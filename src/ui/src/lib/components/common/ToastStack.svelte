<script lang="ts">
	import { dismissToast, ui } from '$lib/workspace.svelte';
</script>

<div class="pointer-events-none fixed right-4 bottom-4 z-50 flex flex-col gap-2">
	{#each ui.toasts as toast (toast.id)}
		<div
			class="pointer-events-auto flex items-center gap-2 rounded-lg border px-3 py-2 text-sm shadow-lg
				{toast.error ? 'border-loss/40 bg-surface-800 text-loss' : 'border-accent/30 bg-surface-800 text-slate-800'}"
			role="status"
		>
			<span>{toast.message}</span>
			<button
				type="button"
				class="text-slate-500 hover:text-slate-700"
				aria-label="Dismiss notification"
				onclick={() => dismissToast(toast.id)}>✕</button
			>
		</div>
	{/each}
</div>
