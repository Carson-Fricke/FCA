<script lang="ts">
	import Dialog from '$lib/components/shell/Dialog.svelte';

	/** Modal confirmation for deleting one conversation. */
	let {
		title,
		onConfirm,
		onClose
	}: { title: string; onConfirm: () => void; onClose: () => void } = $props();
</script>

<Dialog title="Delete conversation" {onClose}>
	<div class="space-y-4 p-4" data-testid="confirm-delete-conversation">
		<p class="text-sm text-slate-700">
			Delete <span class="font-semibold text-slate-800">“{title}”</span>? This removes the
			conversation and its messages and cannot be undone.
		</p>
		<div class="flex items-center justify-end gap-2 border-t border-surface-700 pt-3">
			<button
				type="button"
				class="rounded-lg border border-surface-600 px-3 py-1.5 text-sm text-slate-600 transition hover:text-slate-800"
				onclick={onClose}
			>
				Cancel
			</button>
			<button
				type="button"
				class="rounded-lg border border-loss/40 bg-loss/15 px-3 py-1.5 text-sm font-semibold text-loss transition hover:bg-loss/25"
				onclick={onConfirm}
				data-testid="confirm-delete-conversation-button"
			>
				Delete
			</button>
		</div>
	</div>
</Dialog>
