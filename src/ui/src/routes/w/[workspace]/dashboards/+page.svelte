<script lang="ts">
	import EmptyState from '$lib/components/common/EmptyState.svelte';
	import WidgetCard from '$lib/components/dashboards/WidgetCard.svelte';
	import { listWidgets, removeWidget } from '$lib/api';
	import { notify, togglePin } from '$lib/workspace.svelte';
	import type { WidgetDefinition } from '$lib/types';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();

	const workspaceId = $derived(data.workspaceId);

	let widgets = $state<WidgetDefinition[]>([]);
	let loaded = $state(false);

	$effect(() => {
		let cancelled = false;
		listWidgets(workspaceId).then((items) => {
			if (!cancelled) {
				widgets = items;
				loaded = true;
			}
		});
		return () => {
			cancelled = true;
		};
	});

	async function remove(widgetId: string): Promise<void> {
		await removeWidget(workspaceId, widgetId);
		widgets = widgets.filter((w) => w.id !== widgetId);
		notify('Widget removed');
	}

	function pin(widget: WidgetDefinition): void {
		togglePin(workspaceId, widget.id);
		widgets = widgets.map((w) => (w.id === widget.id ? { ...w, pinned: !w.pinned } : w));
	}
</script>

<svelte:head><title>Dashboards — Thesis</title></svelte:head>

<div class="mx-auto max-w-6xl space-y-4">
	<header class="flex flex-wrap items-center gap-3">
		<div>
			<h1 class="text-xl font-semibold text-slate-800">Dashboards</h1>
			<p class="text-sm text-slate-500">
				Widget definitions are workspace artifacts, fetched — never authored client-side as state.
			</p>
		</div>
	</header>

	{#if loaded && widgets.length === 0}
		<EmptyState
			title="No widgets yet"
			hint="Ask the agent to create a dashboard, or pin one from a backtest."
		/>
	{:else}
		<div class="grid gap-3 md:grid-cols-2 xl:grid-cols-3" data-testid="widget-grid">
			{#each widgets as widget (widget.id)}
				<WidgetCard
					{widget}
					onPin={() => pin(widget)}
					onRemove={() => remove(widget.id)}
				/>
			{/each}
		</div>
	{/if}
</div>
