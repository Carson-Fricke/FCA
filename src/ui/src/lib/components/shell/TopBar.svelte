<script lang="ts">
	import { relativeTime } from '$lib/format';
	import { fixtureMode, uiEnvironment } from '$lib/api';
	import type { ActivityItem } from '$lib/types';

	/** Top bar: breadcrumbs, environment badge, docs, notifications, copilot toggle. */
	let {
		workspaceId,
		workspaceName,
		page,
		activity,
		copilotHidden,
		onToggleCopilot,
		copilotVisible
	}: {
		workspaceId: string;
		workspaceName: string;
		page: string;
		activity: ActivityItem[];
		copilotHidden: boolean;
		copilotVisible: boolean;
		onToggleCopilot: () => void;
	} = $props();

	let notificationsOpen = $state(false);
	let unread = $state(true);
	let holder = $state<HTMLDivElement | null>(null);

	function onWindowPointerDown(event: MouseEvent): void {
		if (holder && !holder.contains(event.target as Node)) notificationsOpen = false;
	}

	const pageLabels: Record<string, string> = {
		overview: 'Overview',
		chats: 'AI chats',
		strategies: 'Strategies',
		backtests: 'Backtests',
		deployments: 'Deployments',
		dashboards: 'Dashboards',
		screener: 'Screener',
		'workspace-state': 'Workspace state'
	};
</script>

<svelte:window onpointerdown={onWindowPointerDown} />

<header class="flex h-14 shrink-0 items-center gap-3 border-b border-surface-700 bg-surface-900 px-4">
	<nav class="flex min-w-0 items-center gap-1.5 text-sm text-slate-500" aria-label="Breadcrumb">
		<a href="/w" class="hover:text-slate-300">Workspace</a>
		<span aria-hidden="true">›</span>
		<a href="/w/{workspaceId}/overview" class="truncate hover:text-slate-300">{workspaceName}</a>
		{#if page !== 'overview'}
			<span aria-hidden="true">›</span>
			<span class="truncate text-slate-300">{pageLabels[page] ?? page}</span>
		{/if}
	</nav>

	<div class="ml-auto flex items-center gap-2">
		{#if fixtureMode || uiEnvironment === 'dev'}
			<span
				class="rounded-full border border-warn/40 bg-warn/10 px-2.5 py-1 text-[11px] font-medium text-warn"
				data-testid="environment-badge"
				title="This workspace runs against simulated surfaces"
			>
				<span class="mr-1 inline-block h-1.5 w-1.5 rounded-full bg-warn" aria-hidden="true"></span>
				Simulation environment
			</span>
		{/if}
		<a
			href="https://github.com/"
			class="hidden items-center gap-1 text-xs text-slate-500 hover:text-slate-300 sm:flex"
		>
			Docs <span aria-hidden="true">↗</span>
		</a>
		<span class="hidden h-4 w-px bg-white/10 sm:block"></span>

		<div class="relative" bind:this={holder}>
			<button
				type="button"
				class="relative rounded-lg border border-surface-700 px-2 py-1.5 text-slate-400 transition hover:border-surface-600 hover:text-slate-200 {notificationsOpen
					? 'border-accent/40 text-accent'
					: ''}"
				aria-label="Notifications"
				onclick={() => (notificationsOpen = !notificationsOpen)}
				data-testid="notifications-button"
			>
				<span aria-hidden="true">◔</span>
				{#if unread}
					<span
						class="absolute top-1 right-1 h-1.5 w-1.5 rounded-full bg-accent"
						aria-hidden="true"
					></span>
				{/if}
			</button>
			{#if notificationsOpen}
				<div
					class="absolute top-full right-0 z-40 mt-1 w-80 rounded-xl border border-surface-600 bg-surface-850 p-2 shadow-2xl"
					data-testid="notifications-popover"
				>
					<div class="mb-1 flex items-center justify-between px-1">
						<h3 class="text-xs font-semibold text-slate-300">Workspace updates</h3>
						<button
							type="button"
							class="text-[11px] text-accent hover:underline"
							onclick={() => (unread = false)}
						>
							{unread ? 'Mark all read' : 'All read'}
						</button>
					</div>
					{#each activity.slice(0, 5) as item (item.id)}
						<div class="rounded-lg px-2 py-1.5 hover:bg-surface-800/60">
							<p class="text-xs font-medium text-slate-200">{item.title}</p>
							<p class="text-[11px] text-slate-500">{item.description}</p>
							<p class="font-mono text-[10px] text-slate-600">{relativeTime(item.created_at)}</p>
						</div>
					{:else}
						<p class="px-2 py-3 text-center text-xs text-slate-500">
							You’re all caught up. Your activity will appear here.
						</p>
					{/each}
				</div>
			{/if}
		</div>

		{#if !copilotHidden}
			<button
				type="button"
				class="rounded-lg border px-2 py-1.5 text-xs transition
					{copilotVisible
					? 'border-accent/40 bg-accent/10 text-accent'
					: 'border-surface-700 text-slate-400 hover:border-surface-600 hover:text-slate-200'}"
				onclick={onToggleCopilot}
				data-testid="copilot-toggle"
				title="Toggle workspace copilot"
			>
				✦ Copilot
			</button>
		{/if}
	</div>
</header>
