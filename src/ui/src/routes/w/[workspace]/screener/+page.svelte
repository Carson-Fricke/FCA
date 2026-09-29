<script lang="ts">
	import EmptyState from '$lib/components/common/EmptyState.svelte';
	import ScreenerPanel from '$lib/components/screener/ScreenerPanel.svelte';
	import { listScreeners } from '$lib/api';
	import type { ScreenerDefinition } from '$lib/types';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();

	const workspaceId = $derived(data.workspaceId);

	let saved = $state<ScreenerDefinition[] | null>(null);
	let loadError = $state<string | null>(null);

	$effect(() => {
		let cancelled = false;
		saved = null;
		loadError = null;
		listScreeners(workspaceId)
			.then((items) => {
				if (!cancelled) saved = items;
			})
			.catch((error) => {
				if (!cancelled) loadError = error instanceof Error ? error.message : String(error);
			});
		return () => {
			cancelled = true;
		};
	});
</script>

<svelte:head><title>Screener — Thesis</title></svelte:head>

<div class="space-y-4">
	{#if loadError}
		<EmptyState title="Saved screeners unavailable" hint={loadError} />
	{/if}
	{#key workspaceId}
		<ScreenerPanel {workspaceId} initialSaved={saved ?? []} />
	{/key}
</div>
