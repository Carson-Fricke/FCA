<script lang="ts">
	import WorkspaceSwitcher from './WorkspaceSwitcher.svelte';
	import { pinnedWidgets, togglePin } from '$lib/workspace.svelte';
	import type { WidgetDefinition, WorkspaceSnapshot } from '$lib/types';

	/** Persistent sidebar: brand, switcher, search, nav with counts, pinned, footer. */
	let {
		snapshot,
		workspaceId,
		page,
		widgets,
		onSearch,
		onSwitch,
		onCreateWorkspace,
		onSettings,
		canConfigureBackend
	}: {
		snapshot: WorkspaceSnapshot;
		workspaceId: string;
		page: string;
		widgets: WidgetDefinition[];
		onSearch: () => void;
		onSwitch: (id: string) => void;
		onCreateWorkspace: () => void;
		onSettings: () => void;
		canConfigureBackend: boolean;
	} = $props();

	const NAV: { id: string; label: string; icon: string; count?: keyof WorkspaceSnapshot['counts'] }[] = [
		{ id: 'overview', label: 'Overview', icon: '◎' },
		{ id: 'conversations', label: 'Conversations', icon: '❍', count: 'chats' },
		{ id: 'strategies', label: 'Strategies', icon: '❮', count: 'strategies' },
		{ id: 'backtests', label: 'Backtests', icon: '⛃', count: 'backtests' },
		{ id: 'deployments', label: 'Deployments', icon: '➤', count: 'deployments' },
		{ id: 'dashboards', label: 'Dashboards', icon: '▤', count: 'dashboards' },
		{ id: 'screener', label: 'Screener', icon: '⌕', count: 'screeners' },
		{ id: 'workspace-state', label: 'Workspace state', icon: '⚙' }
	];

	const pinned = $derived(
		pinnedWidgets(workspaceId)
			.map((id) => widgets.find((w) => w.id === id))
			.filter((w): w is WidgetDefinition => Boolean(w))
	);
</script>

<aside
	class="flex w-60 shrink-0 flex-col border-r border-surface-700 bg-surface-900"
	data-testid="sidebar"
>
	<div class="flex items-center gap-2 px-4 pt-4 pb-3">
		<span
			class="flex h-8 w-8 items-center justify-center rounded-lg border border-surface-600 bg-surface-800 font-mono text-sm font-bold text-slate-700"
			aria-hidden="true"
		>
			Θ
		</span>
		<span class="text-lg font-semibold tracking-tight text-slate-800">thesis<span class="text-accent">.</span></span>
	</div>

	<div class="px-3">
		<WorkspaceSwitcher
			workspaces={snapshot.workspaces}
			currentId={workspaceId}
			{onSwitch}
			onCreate={onCreateWorkspace}
		/>
	</div>

	<div class="px-3 pt-3">
		<button
			type="button"
			class="flex w-full items-center gap-2 rounded-lg border border-surface-700 bg-surface-800 px-2.5 py-2 text-left text-sm text-slate-500 transition hover:border-surface-600 hover:text-slate-500"
			onclick={onSearch}
			data-testid="search-trigger"
		>
			<span aria-hidden="true">⌕</span>
			<span class="flex-1">Search anything…</span>
			<kbd class="rounded border border-surface-600 bg-surface-900 px-1 font-mono text-[10px]">⌘K</kbd>
		</button>
	</div>

	<div class="mt-4 px-3 text-[11px] font-semibold tracking-wider text-slate-500 uppercase">
		Workspace
	</div>
	<nav class="mt-1 flex-1 overflow-y-auto px-3" aria-label="Primary">
		<ul class="space-y-0.5">
			{#each NAV as item (item.id)}
				<li>
					<a
						href="/w/{workspaceId}/{item.id}"
						class="flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-[15px] transition
							{page === item.id
							? 'bg-surface-800 font-medium text-slate-800'
							: 'text-slate-500 hover:bg-surface-800 hover:text-slate-700'}"
						aria-current={page === item.id ? 'page' : undefined}
						data-testid="nav-{item.id}"
					>
						<span class="w-4 text-center text-slate-500" aria-hidden="true">{item.icon}</span>
						<span class="flex-1">{item.label}</span>
						{#if item.count && snapshot.counts[item.count] > 0}
							<span
								class="rounded-full bg-surface-800 px-1.5 font-mono text-[11px] {page === item.id
									? 'text-slate-700'
									: 'text-slate-500'}"
							>
								{snapshot.counts[item.count]}
							</span>
						{/if}
					</a>
				</li>
			{/each}
		</ul>

		<div class="mt-5 mb-1 flex items-center justify-between px-2.5">
			<span class="text-[10px] font-semibold tracking-wider text-slate-500 uppercase">Pinned</span>
		</div>
		<ul class="space-y-0.5 pb-4">
			{#each pinned as widget (widget.id)}
				<li class="group flex items-center">
					<a
						href="/w/{workspaceId}/dashboards"
						class="flex flex-1 items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-sm text-slate-500 transition hover:bg-surface-800 hover:text-slate-800"
					>
						<span class="w-4 text-center text-xs" aria-hidden="true">▤</span>
						<span class="truncate">{widget.title}</span>
					</a>
					<button
						type="button"
						class="mr-1 hidden px-1 text-[10px] text-slate-500 group-hover:block hover:text-loss"
						title="Unpin"
						onclick={() => togglePin(workspaceId, widget.id)}
					>
						✕
					</button>
				</li>
			{:else}
				<li class="px-2.5 text-[11px] text-slate-500">Your favorites will feel at home here.</li>
			{/each}
		</ul>
	</nav>

	<div class="border-t border-surface-700 px-4 py-3" data-testid="sidebar-footer">
		<div class="flex items-center gap-2 text-xs">
			<span class="relative flex h-2 w-2" aria-hidden="true">
				<span
					class="absolute inline-flex h-full w-full animate-ping rounded-full bg-profit opacity-60"
				></span>
				<span class="relative inline-flex h-2 w-2 rounded-full bg-profit"></span>
			</span>
			<span class="font-medium {snapshot.overview.health === 'operational' ? 'text-profit' : 'text-warn'}">
				{snapshot.overview.health === 'operational' ? 'All systems operational' : 'Degraded surfaces'}
			</span>
		</div>
		<div class="mt-2 flex items-center gap-2">
			<span
				class="flex h-7 w-7 items-center justify-center rounded-full border border-surface-600 bg-surface-800 font-mono text-[10px] text-slate-700"
			>
				AM
			</span>
			<span class="min-w-0 flex-1">
				<span class="block truncate text-xs font-medium text-slate-700">Alex Morgan</span>
				<span class="block truncate text-[10px] text-slate-500">Personal account</span>
			</span>
			{#if canConfigureBackend}
				<button
					type="button"
					class="rounded border border-surface-600 px-1.5 py-0.5 text-[10px] text-slate-500 hover:text-slate-700"
					onclick={onSettings}
					title="Choose the active backend"
					data-testid="settings-trigger"
				>
					Settings
				</button>
			{/if}
		</div>
	</div>
</aside>
