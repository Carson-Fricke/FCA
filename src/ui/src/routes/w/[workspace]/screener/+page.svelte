<script lang="ts">
	import ScreenerPanel from '$lib/components/screener/ScreenerPanel.svelte';
	import { listScreeners, listStrategies } from '$lib/api';
	import type { ScreenerDefinition, Strategy } from '$lib/types';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();

	const workspaceId = $derived(data.workspaceId);

	let saved = $state<ScreenerDefinition[]>([]);
	let strategies = $state<Strategy[]>([]);

	$effect(() => {
		let cancelled = false;
		listScreeners(workspaceId).then((items) => {
			if (!cancelled) saved = items;
		});
		listStrategies(workspaceId).then((items) => {
			if (!cancelled) strategies = items;
		});
		return () => {
			cancelled = true;
		};
	});
</script>

<svelte:head><title>Screener — Thesis</title></svelte:head>

<div class="mx-auto max-w-6xl space-y-4">
	<header>
		<h1 class="text-xl font-semibold text-white">Screener</h1>
		<p class="text-sm text-slate-500">
			Volume/liquidity presets; saved screeners persist as workspace artifacts.
		</p>
	</header>

	<ScreenerPanel
		{workspaceId}
		initialSaved={saved}
		strategies={strategies.map((s) => ({ id: s.id, name: s.name, symbol: s.symbol }))}
	/>
</div>
