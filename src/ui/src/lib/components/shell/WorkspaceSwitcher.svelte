<script lang="ts">
	import type { WorkspaceSummary } from '$lib/types';

	/** Workspace switcher popover with avatar/initials + create-workspace. */
	let {
		workspaces,
		currentId,
		onSwitch,
		onCreate,
		onDelete,
		compact = false
	}: {
		workspaces: WorkspaceSummary[];
		currentId: string;
		onSwitch: (id: string) => void;
		onCreate: () => void;
		onDelete?: (workspace: WorkspaceSummary) => void;
		compact?: boolean;
	} = $props();

	let open = $state(false);
	let holder = $state<HTMLDivElement | null>(null);

	const current = $derived(workspaces.find((w) => w.id === currentId) ?? workspaces[0]);

	function onWindowPointerDown(event: MouseEvent): void {
		if (holder && !holder.contains(event.target as Node)) open = false;
	}
</script>

<svelte:window onpointerdown={onWindowPointerDown} onkeydown={(event) => { if (event.key === 'Escape') open = false; }} />

<div class="relative" bind:this={holder} data-testid="workspace-switcher">
	<button
		type="button"
		class="flex w-full items-center gap-2 rounded-lg border border-surface-700 bg-surface-850/70 px-2.5 py-2 text-left transition hover:border-surface-600"
		class:compact
		aria-expanded={open}
		aria-label={compact ? `${current?.name ?? 'Workspace'} · Switch workspace` : undefined}
		title={compact ? `${current?.name ?? 'Workspace'} · Switch workspace` : undefined}
		onclick={() => (open = !open)}
		data-testid="workspace-switcher-button"
	>
		<span
			class="flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-accent/30 bg-accent/10 font-mono text-[11px] font-semibold text-accent"
		>
			{current?.initials}
		</span>
		{#if !compact}<span class="min-w-0 flex-1">
			<span class="block truncate text-sm font-semibold text-slate-800">{current?.name}</span>
			<span class="block truncate text-[11px] text-slate-500">{current?.description}</span>
		</span>
		<span class="text-slate-500" aria-hidden="true">⇅</span>{/if}
	</button>

	{#if open}
		<div
			class="absolute top-full right-0 left-0 z-40 mt-1 rounded-xl border border-surface-600 bg-surface-850 p-1 shadow-2xl"
			class:compact-menu={compact}
			data-testid="workspace-menu"
		>
			<div class="px-2 py-1.5 text-[10px] font-semibold tracking-wider text-slate-500 uppercase">
				Your workspaces
			</div>
			{#each workspaces as workspace (workspace.id)}
				<button
					type="button"
					class="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left transition
						{workspace.id === currentId ? 'bg-surface-800 text-slate-800' : 'text-slate-700 hover:bg-surface-800/60'}"
					onclick={() => {
						open = false;
						if (workspace.id !== currentId) onSwitch(workspace.id);
					}}
					data-testid="workspace-option"
					data-workspace={workspace.id}
				>
					<span
						class="flex h-6 w-6 shrink-0 items-center justify-center rounded border border-surface-600 bg-surface-900 font-mono text-[10px] {workspace.id ===
						currentId
							? 'text-accent'
							: 'text-slate-500'}"
					>
						{workspace.initials}
					</span>
					<span class="min-w-0 flex-1">
						<span class="block truncate text-sm">{workspace.name}</span>
						<span class="block truncate text-[10px] text-slate-500">{workspace.description}</span>
					</span>
					{#if workspace.id === currentId}
						<span class="text-accent" aria-hidden="true">✓</span>
					{/if}
				</button>
			{/each}
			<div class="my-1 border-t border-surface-700"></div>
			<button
				type="button"
				class="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-accent transition hover:bg-accent/10"
				onclick={() => {
					open = false;
					onCreate();
				}}
				data-testid="create-workspace-trigger"
			>
				<span aria-hidden="true">＋</span>
				<span class="text-sm">Create workspace</span>
			</button>
			{#if current && onDelete}
				<button type="button"
					class="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-sm text-loss transition hover:bg-loss/10"
					onclick={() => { open = false; onDelete?.(current); }} data-testid="delete-workspace-trigger">
					Delete workspace…
				</button>
			{/if}
		</div>
	{/if}
</div>

<style>
	button.compact{padding:0;justify-content:center;width:38px;min-height:36px;border-color:transparent;background:transparent}.compact-menu{inset:0 auto auto calc(100% + 10px);margin:0;width:280px;max-width:calc(100vw - 76px)}
	@media(pointer:coarse){button.compact{min-height:44px}}
</style>
