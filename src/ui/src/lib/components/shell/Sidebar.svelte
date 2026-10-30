<script lang="ts">
	import WorkspaceSwitcher from './WorkspaceSwitcher.svelte';
	import { pinnedWidgets, togglePin } from '$lib/workspace.svelte';
	import type { WidgetDefinition, WorkspaceSnapshot, WorkspaceSummary } from '$lib/types';

	/** Navigation rail and expandable labels; all actions retain the same shell owners. */
	let {
		snapshot,
		ready,
		workspaceId,
		page,
		widgets,
		onSearch,
		onSwitch,
		onCreateWorkspace,
		onDeleteWorkspace,
		onSettings,
		canConfigureBackend,
		compact = false,
		onToggleCompact = () => {}
	}: {
		snapshot: WorkspaceSnapshot;
		ready: boolean;
		workspaceId: string;
		page: string;
		widgets: WidgetDefinition[];
		onSearch: () => void;
		onSwitch: (id: string) => void;
		onCreateWorkspace: () => void;
		onDeleteWorkspace: (workspace: WorkspaceSummary) => void;
		onSettings: () => void;
		canConfigureBackend: boolean;
		compact?: boolean;
		onToggleCompact?: () => void;
	} = $props();

	const NAV: { id: string; label: string; count?: keyof WorkspaceSnapshot['counts'] }[] = [
		{ id: 'overview', label: 'Overview' },
		{ id: 'conversations', label: 'Conversations', count: 'chats' },
		{ id: 'strategies', label: 'Strategies', count: 'strategies' },
		{ id: 'backtests', label: 'Backtests', count: 'backtests' },
		{ id: 'deployments', label: 'Deployments', count: 'deployments' },
		{ id: 'dashboards', label: 'Dashboards', count: 'dashboards' },
		{ id: 'screener', label: 'Screener', count: 'screeners' },
		{ id: 'artifacts', label: 'Workspace artifacts' }
	];

	const pinned = $derived(
		pinnedWidgets(workspaceId)
			.map((id) => widgets.find((w) => w.id === id))
			.filter((w): w is WidgetDefinition => Boolean(w))
	);
</script>

<svelte:window onkeydown={(event) => { if (event.key === 'Escape' && !compact) onToggleCompact(); }} />
{#if !compact}<button type="button" class="sidebar-scrim" aria-label="Close navigation" onclick={onToggleCompact}></button>{/if}
<aside
	class="workspace-sidebar flex shrink-0 flex-col border-r border-surface-700 bg-surface-900"
	class:compact
	data-testid="sidebar"
>
	<div class="sidebar-brand flex items-center gap-2 px-4 pt-4 pb-3">
		<button type="button" class="brand-toggle" onclick={onToggleCompact} aria-label={compact ? 'Expand navigation' : 'Collapse navigation'} title={compact ? 'Expand navigation' : 'Collapse navigation'} aria-expanded={!compact} data-testid="sidebar-toggle"><span aria-hidden="true">Θ</span></button>
		{#if !compact}<span class="text-lg font-semibold tracking-tight text-slate-800">thesis<span class="text-accent">.</span></span>{/if}
	</div>

	<div class="sidebar-switcher px-3">
		<WorkspaceSwitcher
			{compact}
			workspaces={snapshot.workspaces}
			currentId={workspaceId}
			{onSwitch}
			onCreate={onCreateWorkspace}
			onDelete={onDeleteWorkspace}
		/>
	</div>

	<div class="sidebar-search px-3 pt-3">
		<button
			type="button"
			class="search-button flex w-full items-center gap-2 rounded-lg border border-surface-700 bg-surface-800 px-2.5 py-2 text-left text-sm text-slate-500 transition hover:border-surface-600 hover:text-slate-500"
			onclick={onSearch}
			aria-label="Search workspace"
			title="Search workspace (Ctrl+K)"
			data-testid="search-trigger"
		>
			{@render icon('search')}
			{#if !compact}<span class="flex-1">Search anything…</span><kbd class="rounded border border-surface-600 bg-surface-900 px-1 font-mono text-[10px]">⌘K</kbd>{/if}
		</button>
	</div>

	{#if !compact}<div class="mt-4 px-3 text-[11px] font-semibold tracking-wider text-slate-500 uppercase">
		Workspace
	</div>{/if}
	<nav class="sidebar-nav mt-1 flex-1 overflow-y-auto px-3" aria-label="Primary">
		<ul class="space-y-0.5">
			{#each NAV as item (item.id)}
				<li>
					<a
						href="/w/{workspaceId}/{item.id}"
						class="nav-item flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-[15px] transition
							{page === item.id
							? 'bg-surface-800 font-medium text-slate-800'
							: 'text-slate-500 hover:bg-surface-800 hover:text-slate-700'}"
						aria-current={page === item.id ? 'page' : undefined}
						aria-label={item.label}
						title={compact ? item.label : undefined}
						data-testid="nav-{item.id}"
					>
						{@render icon(item.id)}
						{#if !compact}<span class="flex-1">{item.label}</span>{/if}
						{#if !compact && item.count && snapshot.counts[item.count] > 0}
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

		{#if !compact}<div class="mt-5 mb-1 flex items-center justify-between px-2.5">
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
		</ul>{/if}
	</nav>

	{#if compact}
		<div class="compact-footer" data-testid="sidebar-footer">
			<span class="health-dot" class:degraded={ready && snapshot.overview.health !== 'operational'} title={!ready ? 'Checking status…' : snapshot.overview.health === 'operational' ? 'All systems operational' : 'Degraded surfaces'} role="img" aria-label={!ready ? 'Checking status' : snapshot.overview.health === 'operational' ? 'All systems operational' : 'Degraded surfaces'}></span>
			{#if canConfigureBackend}<button type="button" class="rail-action" onclick={onSettings} aria-label="Backend settings" title="Backend settings" data-testid="settings-trigger">{@render icon('settings')}</button>{/if}
			<span class="account-initials" title="Alex Morgan · Personal account">AM</span>
		</div>
	{:else}
	<div class="border-t border-surface-700 px-4 py-3" data-testid="sidebar-footer">
		<div class="flex items-center gap-2 text-xs">
			<span class="relative flex h-2 w-2" aria-hidden="true">
				<span
					class="absolute inline-flex h-full w-full animate-ping rounded-full bg-profit opacity-60"
				></span>
				<span class="relative inline-flex h-2 w-2 rounded-full bg-profit"></span>
			</span>
			<span class="font-medium {!ready
				? 'text-slate-500'
				: snapshot.overview.health === 'operational'
					? 'text-profit'
					: 'text-warn'}">
				{!ready
					? 'Checking status…'
					: snapshot.overview.health === 'operational'
						? 'All systems operational'
						: 'Degraded surfaces'}
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
	{/if}
</aside>

{#snippet icon(name: string)}
	<svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
		{#if name === 'overview'}<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>
		{:else if name === 'conversations'}<path d="M4 4h13v10H9l-5 4zM10 18h5l5 3V9"/>
		{:else if name === 'strategies'}<circle cx="5" cy="17" r="2"/><circle cx="12" cy="6" r="2"/><circle cx="20" cy="15" r="2"/><path d="m6 15 5-7m3 0 5 5M7 17h6l5-1"/>
		{:else if name === 'backtests'}<path d="M4 20h17M5 16V9m6 7V5m6 11v-6M3 5l4-2m2 7 5-5 6 2"/>
		{:else if name === 'deployments'}<path d="m5 4 16 8-16 8 3-8zM8 12h13"/>
		{:else if name === 'dashboards'}<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18m-11 0v10"/>
		{:else if name === 'artifacts'}<path d="M3 7V5h7l2 3h9l-3 12H3zM3 12h17"/>
		{:else if name === 'screener'}<path d="M3 4h18l-7 8v7l-4 2v-9z"/>
		{:else if name === 'settings'}<circle cx="12" cy="12" r="3"/><path d="m9 3-1 3-3 1-2 3 2 2-1 3 2 3 3-1 3 3 3-2 3 1 2-3-1-3 2-2-2-3-3-1-1-3z"/>
		{:else}<circle cx="10" cy="10" r="6"/><path d="m15 15 5 5"/>{/if}
	</svg>
{/snippet}

<style>
	.workspace-sidebar{width:240px;min-height:0;position:relative}.workspace-sidebar.compact{width:56px;background:var(--color-surface-800)}
	.brand-toggle{display:flex;align-items:center;justify-content:center;width:30px;height:30px;flex-shrink:0;border:1px solid var(--color-surface-600);border-radius:6px;background:var(--color-surface-900);font-size:15px;color:var(--color-accent);cursor:pointer}.brand-toggle:hover{border-color:var(--color-accent)}
	.compact .sidebar-brand{justify-content:center;padding:16px 0 12px}.compact .sidebar-switcher{padding:0 9px}.compact .sidebar-search{padding:10px 9px}.compact .search-button{justify-content:center;width:38px;height:34px;border:0;background:transparent;padding:0}.compact .search-button:hover{background:var(--color-surface-900);color:var(--color-accent)}
	.compact .sidebar-nav{padding:0 9px;margin-top:5px}.compact .nav-item{width:38px;height:38px;justify-content:center;padding:0;margin-bottom:3px}.compact .nav-item[aria-current=page]{color:var(--color-accent);background:color-mix(in srgb,var(--color-accent) 9%,var(--color-surface-900))}
	.nav-icon{width:16px;height:16px;flex-shrink:0}.compact-footer{display:flex;flex-direction:column;align-items:center;gap:10px;padding:13px 8px;border-top:1px solid var(--color-surface-700)}.rail-action{display:flex;align-items:center;justify-content:center;width:36px;height:32px;border-radius:5px;color:var(--color-slate-400);cursor:pointer}.rail-action:hover{background:var(--color-surface-900);color:var(--color-accent)}
	.account-initials{display:flex;align-items:center;justify-content:center;width:27px;height:27px;border:1px solid var(--color-surface-600);border-radius:50%;font-size:10px;color:var(--color-slate-400);background:var(--color-surface-900)}.health-dot{width:6px;height:6px;border-radius:50%;background:var(--color-profit)}.health-dot.degraded{background:var(--color-warn)}.sidebar-scrim{display:none}
	@media(max-width:700px){.workspace-sidebar:not(.compact){position:fixed;inset:0 auto 0 0;z-index:45;box-shadow:5px 0 20px rgb(0 0 0 / .08)}.sidebar-scrim{display:block;position:fixed;inset:0;z-index:44;background:rgb(0 0 0 / .15)}}
	@media(pointer:coarse){.compact .nav-item{height:44px}.compact .search-button,.rail-action,.brand-toggle{min-height:44px}}
</style>
