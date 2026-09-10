<script lang="ts">
	import { page } from '$app/state';
	import MetricCard from '$lib/components/common/MetricCard.svelte';
	import EmptyState from '$lib/components/common/EmptyState.svelte';
	import { relativeTime } from '$lib/format';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();

	const activity = $derived(data.snapshot.overview.activity);
	const openChats = $derived(data.snapshot.overview.openChats);
	const counts = $derived(data.snapshot.counts);
	const workspaceId = $derived(page.params.workspace ?? '');
</script>

<svelte:head><title>Overview — Thesis</title></svelte:head>

<div class="mx-auto max-w-6xl space-y-5">
	<header>
		<h1 class="text-xl font-semibold text-white">Overview</h1>
		<p class="text-sm text-slate-500">
			Workspace landing for <span class="text-accent">{data.snapshot.overview.workspace.name}</span>.
		</p>
	</header>

	<div class="grid grid-cols-2 gap-3 lg:grid-cols-4">
		<MetricCard label="Strategies" value={counts.strategies} />
		<MetricCard label="Backtests" value={counts.backtests} />
		<MetricCard label="Deployments" value={counts.deployments} />
		<MetricCard label="Open chats" value={counts.chats} />
	</div>

	<div class="grid gap-4 lg:grid-cols-3">
		<section class="rounded-xl border border-surface-700 bg-surface-900 p-4 lg:col-span-2">
			<h2 class="mb-3 text-sm font-semibold text-slate-200">Recent activity</h2>
			{#if activity.length}
				<ul class="space-y-2">
					{#each activity as item (item.id)}
						<li class="flex items-center gap-3 rounded-lg border border-surface-700 bg-surface-900 px-3 py-2">
							<span
								class="h-1.5 w-1.5 shrink-0 rounded-full
									{item.type === 'backtest'
									? 'bg-accent'
									: item.type === 'deploy'
										? 'bg-profit'
										: item.type === 'strategy'
											? 'bg-warn'
											: 'bg-surface-600'}"
								aria-hidden="true"
							></span>
							<div class="min-w-0">
								<p class="truncate text-sm text-slate-200">{item.title}</p>
								<p class="truncate text-xs text-slate-500">{item.description}</p>
							</div>
							<span class="ml-auto shrink-0 font-mono text-[10px] text-slate-600">
								{relativeTime(item.created_at)}
							</span>
						</li>
					{/each}
				</ul>
			{:else}
				<EmptyState title="No activity yet" hint="Chats, backtests, and deployments will land here." />
			{/if}
		</section>

		<section class="space-y-4">
			<div class="rounded-xl border border-surface-700 bg-surface-900 p-4">
				<h2 class="mb-3 text-sm font-semibold text-slate-200">Open chats</h2>
				{#if openChats.length}
					<ul class="space-y-2">
						{#each openChats as chat (chat.chat_id)}
							<li>
								<a
									href="/w/{workspaceId}/chats?chat={chat.chat_id}"
									class="block rounded-lg border border-surface-700 bg-surface-900 px-3 py-2 hover:border-accent/30"
								>
									<p class="truncate text-sm text-slate-200">{chat.title}</p>
									<p class="text-[11px] text-slate-500">
										{chat.kind === 'investigation' ? 'investigation' : 'chat'} ·
										{relativeTime(chat.updated_at ?? chat.created_at)}
									</p>
								</a>
							</li>
						{/each}
					</ul>
				{:else}
					<EmptyState title="Your next good idea starts with a conversation." />
				{/if}
			</div>

			<div class="rounded-xl border border-surface-700 bg-surface-900 p-4">
				<h2 class="mb-2 text-sm font-semibold text-slate-200">Workspace health</h2>
				<p class="flex items-center gap-2 text-sm">
					<span
						class="h-2 w-2 rounded-full {data.snapshot.overview.health === 'operational'
							? 'bg-profit'
							: 'bg-warn'}"
						aria-hidden="true"
					></span>
					<span class="text-slate-300">
						{data.snapshot.overview.health === 'operational'
							? 'All systems operational'
							: 'Degraded surfaces — see deployments'}
					</span>
				</p>
				<p class="mt-2 font-mono text-[10px] text-slate-600">{counts.dashboards} dashboards · {counts.screeners} saved screeners</p>
			</div>
		</section>
	</div>
</div>
