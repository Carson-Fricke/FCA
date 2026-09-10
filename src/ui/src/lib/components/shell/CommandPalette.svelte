<script lang="ts">
	import Dialog from './Dialog.svelte';
	import type { Strategy, WidgetDefinition } from '$lib/types';

	/** Command palette (⌘K): go-to pages, strategies, dashboards, quick actions. */
	let {
		workspaceId,
		strategies,
		widgets,
		onClose
	}: {
		workspaceId: string;
		strategies: Strategy[];
		widgets: WidgetDefinition[];
		onClose: () => void;
	} = $props();

	const PAGES = [
		{ id: 'overview', label: 'Overview' },
		{ id: 'chats', label: 'Conversations' },
		{ id: 'strategies', label: 'Strategies' },
		{ id: 'backtests', label: 'Backtests' },
		{ id: 'deployments', label: 'Deployments' },
		{ id: 'dashboards', label: 'Dashboards' },
		{ id: 'screener', label: 'Screener' },
		{ id: 'workspace-state', label: 'Workspace state' }
	];

	let query = $state('');
	const q = $derived(query.toLowerCase());

	const pages = $derived(PAGES.filter((p) => p.label.toLowerCase().includes(q)));
	const matchedStrategies = $derived(
		strategies.filter((s) => `${s.name} ${s.symbol}`.toLowerCase().includes(q)).slice(0, 6)
	);
	const matchedWidgets = $derived(
		widgets.filter((w) => w.title.toLowerCase().includes(q)).slice(0, 4)
	);
	const nothing = $derived(!pages.length && !matchedStrategies.length && !matchedWidgets.length);

	function go(path: string): void {
		onClose();
		window.location.assign(path);
	}
</script>

<Dialog title="Find your next move" {onClose}>
	<div class="p-2" data-testid="command-palette">
		<label class="flex items-center gap-2 rounded-lg border border-surface-600 bg-surface-950 px-3 py-2">
			<span class="text-slate-500" aria-hidden="true">⌕</span>
			<input
				type="text"
				class="w-full bg-transparent text-sm text-slate-800 outline-none placeholder:text-slate-500"
				placeholder="Search strategies, pages, dashboards…"
				bind:value={query}
			/>
			<kbd class="rounded border border-surface-600 px-1 font-mono text-[10px] text-slate-500">ESC</kbd>
		</label>

		<div class="mt-2 max-h-80 overflow-y-auto">
			{#if pages.length}
				<div class="px-2 py-1 text-[10px] font-semibold tracking-wider text-slate-500 uppercase">Go to</div>
				{#each pages as page (page.id)}
					<button
						type="button"
						class="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-sm text-slate-700 hover:bg-surface-800"
						onclick={() => go(`/w/${workspaceId}/${page.id}`)}
					>
						<span aria-hidden="true">◎</span>{page.label}<span class="ml-auto text-slate-500" aria-hidden="true">→</span>
					</button>
				{/each}
			{/if}
			{#if matchedStrategies.length}
				<div class="px-2 py-1 text-[10px] font-semibold tracking-wider text-slate-500 uppercase">Strategies</div>
				{#each matchedStrategies as strategy (strategy.id)}
					<button
						type="button"
						class="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-sm text-slate-700 hover:bg-surface-800"
						onclick={() => go(`/w/${workspaceId}/strategies/${strategy.id}`)}
					>
						<span class="font-mono text-xs text-accent">{strategy.symbol}</span>
						{strategy.name}
						<span class="ml-auto text-slate-500" aria-hidden="true">↗</span>
					</button>
				{/each}
			{/if}
			{#if matchedWidgets.length}
				<div class="px-2 py-1 text-[10px] font-semibold tracking-wider text-slate-500 uppercase">Dashboards</div>
				{#each matchedWidgets as widget (widget.id)}
					<button
						type="button"
						class="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-sm text-slate-700 hover:bg-surface-800"
						onclick={() => go(`/w/${workspaceId}/dashboards`)}
					>
						<span aria-hidden="true">▤</span>{widget.title}
						<span class="ml-auto text-slate-500" aria-hidden="true">→</span>
					</button>
				{/each}
			{/if}
			{#if nothing}
				<div class="px-3 py-4 text-center">
					<p class="text-sm text-slate-700">No matches for “{query}”</p>
					<p class="text-xs text-slate-500">Try a strategy name or a quick action below.</p>
				</div>
			{/if}

			<div class="px-2 pt-2 pb-1 text-[10px] font-semibold tracking-wider text-slate-500 uppercase">
				Quick actions
			</div>
			<button
				type="button"
				class="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-sm text-slate-700 hover:bg-surface-800"
				onclick={() => go(`/w/${workspaceId}/chats`)}
			>
				<span aria-hidden="true">＋</span>New chat
			</button>
			<button
				type="button"
				class="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-sm text-slate-700 hover:bg-surface-800"
				onclick={() => go(`/w/${workspaceId}/screener`)}
			>
				<span aria-hidden="true">＋</span>New screener
			</button>
		</div>

		<div
			class="mt-1 flex items-center justify-between border-t border-surface-700 px-3 py-2 text-[10px] text-slate-500"
		>
			<span>⌘K to open anytime</span>
			<span>Searching in workspace <strong class="text-slate-500">{workspaceId}</strong></span>
		</div>
	</div>
</Dialog>
