<script lang="ts">
	import Dialog from '$lib/components/shell/Dialog.svelte';

	/** Create one draft strategy registry entry. */
	let {
		onClose,
		onCreate
	}: {
		onClose: () => void;
		onCreate: (input: { name: string; symbol: string; description: string }) => void;
	} = $props();

	let name = $state('');
	let symbol = $state('');
	let description = $state('');

	const canCreate = $derived(name.trim().length > 0 && symbol.trim().length > 0);
	const inputClass =
		'w-full rounded-lg border border-surface-600 bg-surface-950 px-3 py-2 text-sm text-slate-800 outline-none placeholder:text-slate-500 focus:border-accent/50';

	function submit(event: SubmitEvent): void {
		event.preventDefault();
		if (!canCreate) return;
		onCreate({ name: name.trim(), symbol: symbol.trim(), description: description.trim() });
	}
</script>

<Dialog title="New strategy" {onClose}>
	<form class="space-y-3 p-4" data-testid="create-strategy" onsubmit={submit}>
		<label class="block">
			<span class="mb-1 block text-xs font-medium text-slate-700">Name</span>
			<input
				class={inputClass}
				placeholder="Momentum breakout"
				bind:value={name}
				data-testid="strategy-name"
			/>
		</label>
		<label class="block">
			<span class="mb-1 block text-xs font-medium text-slate-700">Symbol</span>
			<input
				class={inputClass}
				placeholder="AAPL"
				bind:value={symbol}
				data-testid="strategy-symbol"
			/>
		</label>
		<label class="block">
			<span class="mb-1 block text-xs font-medium text-slate-700">Description</span>
			<textarea
				class={`${inputClass} min-h-20 resize-y`}
				placeholder="Premise, instruments, and horizon."
				bind:value={description}
				data-testid="strategy-description"
			></textarea>
		</label>
		<div class="flex items-center justify-end gap-2 border-t border-surface-700 pt-3">
			<button
				type="button"
				class="rounded-lg border border-surface-600 px-3 py-1.5 text-sm text-slate-600 transition hover:text-slate-800"
				onclick={onClose}
			>
				Cancel
			</button>
			<button
				type="submit"
				class="rounded-lg border border-accent/40 bg-accent/15 px-3 py-1.5 text-sm font-semibold text-accent transition hover:bg-accent/25 disabled:cursor-not-allowed disabled:opacity-40"
				disabled={!canCreate}
				data-testid="create-strategy-submit"
			>
				Create strategy
			</button>
		</div>
	</form>
</Dialog>
