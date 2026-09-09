<script lang="ts">
	import Dialog from './Dialog.svelte';

	/** Create-workspace dialog: creates a workspace via the API. */
	let {
		onCreate,
		onClose
	}: {
		onCreate: (input: { name: string; description: string }) => void;
		onClose: () => void;
	} = $props();

	let name = $state('');
	let description = $state('');

	function submit(): void {
		if (!name.trim()) return;
		onCreate({ name: name.trim(), description: description.trim() || 'Workspace' });
	}

	function initials(): string {
		return name
			.split(/\s+/)
			.slice(0, 2)
			.map((word) => word[0]?.toUpperCase() ?? '')
			.join('');
	}
</script>

<Dialog title="Create workspace" {onClose}>
	<form class="space-y-3 p-4" onsubmit={(e) => { e.preventDefault(); submit(); }}>
		<label class="block">
			<span class="mb-1 block text-xs text-slate-400">Name</span>
			<input
				type="text"
				class="w-full rounded-lg border border-surface-600/60 bg-surface-950/70 px-3 py-2 text-sm text-slate-200 outline-none focus:border-accent/50"
				bind:value={name}
				placeholder="e.g. Gamma Options"
				data-testid="workspace-name-input"
			/>
		</label>
		<label class="block">
			<span class="mb-1 block text-xs text-slate-400">Description</span>
			<input
				type="text"
				class="w-full rounded-lg border border-surface-600/60 bg-surface-950/70 px-3 py-2 text-sm text-slate-200 outline-none focus:border-accent/50"
				bind:value={description}
				placeholder="What is this workspace for?"
			/>
		</label>
		<div class="flex items-center justify-between">
			<span class="font-mono text-[11px] text-slate-600">initials: {initials() || '—'}</span>
			<button
				type="submit"
				class="rounded-lg border border-accent/40 bg-accent/15 px-3 py-1.5 text-sm font-semibold text-accent hover:bg-accent/25 disabled:opacity-40"
				disabled={!name.trim()}
				data-testid="workspace-create-submit"
			>
				Create workspace
			</button>
		</div>
	</form>
</Dialog>
