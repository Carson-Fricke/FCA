<script lang="ts">
	import CleoPlanPanel from '$lib/components/chat/CleoPlanPanel.svelte';
	import ConsentAuditList from '$lib/components/state/ConsentAuditList.svelte';
	import EmptyState from '$lib/components/common/EmptyState.svelte';
	import EvidenceIndex from '$lib/components/state/EvidenceIndex.svelte';
	import LineageGraph from '$lib/components/state/LineageGraph.svelte';
	import { getWorkspaceState } from '$lib/api';
	import type { WorkspaceStateData } from '$lib/types';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();

	const workspaceId = $derived(data.workspaceId);

	let state = $state<WorkspaceStateData | null>(null);

	$effect(() => {
		let cancelled = false;
		getWorkspaceState(workspaceId).then((item) => {
			if (!cancelled) state = item;
		});
		return () => {
			cancelled = true;
		};
	});
</script>

<svelte:head><title>Workspace state — Thesis</title></svelte:head>

<div class="mx-auto max-w-6xl space-y-5">
	<header>
		<h1 class="text-xl font-semibold text-slate-800">Workspace state</h1>
		<p class="text-sm text-slate-500">
			CLEO projection (read-only), strategy lineage, evidence index, and consent/audit trail.
		</p>
	</header>

	{#if !state}
		<div class="h-40 animate-pulse rounded-xl border border-surface-700 bg-surface-900"></div>
	{:else}
		<section class="space-y-2">
			<h2 class="text-sm font-semibold text-slate-800">CLEO plan &amp; workflow</h2>
			<CleoPlanPanel plan={state.cleo} timeline={state.taskTimeline} />
		</section>

		<section class="space-y-2">
			<h2 class="text-sm font-semibold text-slate-800">Strategy lineage</h2>
			{#if state.lineages.length}
				{#each state.lineages as lineage (lineage.strategy_id)}
					<LineageGraph {lineage} />
				{/each}
			{:else}
				<EmptyState title="No strategy lineage yet" />
			{/if}
		</section>

		<section class="space-y-2">
			<h2 class="text-sm font-semibold text-slate-800">Evidence</h2>
			<EvidenceIndex evidence={state.evidence} />
		</section>

		<section class="space-y-2">
			<h2 class="text-sm font-semibold text-slate-800">Consent &amp; audit</h2>
			<ConsentAuditList entries={state.consentAudit} />
		</section>
	{/if}
</div>
