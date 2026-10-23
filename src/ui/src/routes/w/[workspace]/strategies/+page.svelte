<script lang="ts">
	import { listStrategies } from '$lib/api';
	import type { Strategy } from '$lib/types';
	import type { PageData } from './$types';
	let { data }: { data: PageData } = $props();
	const workspaceId=$derived(data.workspaceId);
	let strategies=$state<Strategy[]>([]);
	let loaded=$state(false);
	let loadError=$state('');
	let refreshKey=$state(0);
	$effect(()=>{
		const ws=workspaceId;void refreshKey;let cancelled=false;
		loaded=false;loadError='';
		listStrategies(ws).then(items=>{if(!cancelled){strategies=items;loaded=true;}}).catch(error=>{if(!cancelled){loadError=error instanceof Error?error.message:'Could not load strategies';loaded=true;}});
		return()=>{cancelled=true;};
	});
</script>
<svelte:head><title>Strategies — Thesis</title></svelte:head>
<div class="mx-auto max-w-6xl space-y-4">
	<header class="flex flex-wrap items-start justify-between gap-3"><div><h1 class="text-xl font-semibold text-slate-800">Strategies</h1><p class="mt-1 text-sm text-slate-500">Authored strategy programs and published bundles, with their exact source and history.</p></div><button type="button" class="rounded-lg border border-surface-600 px-3 py-1.5 text-xs text-slate-600" onclick={()=>refreshKey+=1}>Refresh</button></header>
	<p class="text-xs text-slate-500">{strategies.length} strategies · drafts appear before their first backtest</p>
	{#if loadError}<p role="alert" class="rounded-lg border border-loss/30 bg-loss/5 p-3 text-sm text-loss">{loadError}</p>
	{:else if !loaded}<p role="status" class="py-8 text-sm text-slate-500">Loading authored strategies…</p>
	{:else if strategies.length===0}<p class="py-8 text-center text-sm text-slate-500">No committed strategies in this workspace. Strategies authored during research appear here automatically.</p>
	{:else}
		<div class="overflow-x-auto rounded-xl border border-surface-700"><table class="w-full min-w-[600px] border-collapse text-left text-sm" data-testid="strategy-list">
			<thead class="bg-surface-900 text-xs text-slate-500"><tr><th class="px-4 py-3 font-medium">Strategy</th><th class="px-4 py-3 font-medium">Current revision</th><th class="px-4 py-3 font-medium">Inputs</th><th class="px-4 py-3 font-medium">Status</th><th class="px-4 py-3 font-medium">Backtests</th></tr></thead>
			<tbody>{#each strategies as strategy(strategy.id)}<tr class="border-t border-surface-700 align-top hover:bg-surface-900">
				<td class="px-4 py-4"><a href="/w/{workspaceId}/strategies/{strategy.id}" class="font-medium text-slate-800 hover:text-accent">{strategy.name}</a><p class="mt-1 text-xs text-slate-500">{strategy.description||strategy.id}</p></td>
				<td class="px-4 py-4 font-mono text-xs text-slate-600"><span title={strategy.version}>{strategy.version?.slice(0,12)||strategy.versions.at(-1)?.version||'—'}</span><p class="mt-1 font-sans text-slate-500">{strategy.history?.length??strategy.versions.length} history entries</p></td>
				<td class="px-4 py-4 text-xs text-slate-500">{#if strategy.inputs?.length}{#each strategy.inputs as input}<p>{input.symbols.join(', ')} · {input.interval||input.data_type}</p>{/each}{:else}{strategy.symbol||'See manifest'}{/if}</td>
				<td class="px-4 py-4"><span class="rounded bg-accent/10 px-2 py-1 text-xs text-accent">{strategy.status}</span></td>
				<td class="px-4 py-4"><a class="text-xs text-accent hover:underline" href="/w/{workspaceId}/backtests?strategy_id={encodeURIComponent(strategy.id)}">{strategy.backtest_count??0} executions →</a><p class="mt-1 text-xs text-slate-500">Results belong to individual runs.</p></td>
			</tr>{/each}</tbody>
		</table></div>
	{/if}
</div>
