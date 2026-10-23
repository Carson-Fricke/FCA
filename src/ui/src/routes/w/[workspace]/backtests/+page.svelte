<script lang="ts">
	import { page } from '$app/state';
	import { goto } from '$app/navigation';
	import { listBacktestPage, listStrategies } from '$lib/api';
	import type { BacktestCatalogPage, BacktestCatalogRow, Strategy } from '$lib/types';
	import type { PageData } from './$types';
	let { data }: { data: PageData } = $props();
	const workspaceId = $derived(data.workspaceId);
	const strategyId = $derived(page.url.searchParams.get('strategy_id') ?? '');
	const offset = $derived(Math.max(0, Number(page.url.searchParams.get('offset')) || 0));
	let catalog = $state<BacktestCatalogPage>({items:[],total:0,offset:0,limit:50});
	let strategies = $state<Strategy[]>([]);
	let statusFilter = $state('all');
	let loading = $state(true);
	let loadError = $state('');
	let refreshKey = $state(0);
	let loadedScope = '';
	const terminal = (state:string) => ['succeeded','failed','cancelled'].includes(state);
	const duration = (seconds:number|null) => seconds===null ? '—' : seconds<60 ? `${Math.round(seconds)}s` : `${(seconds/60).toFixed(1)} min`;
	const date = (value:string|null) => value?.slice(0,10) ?? '—';
	const filtered = $derived(catalog.items.filter(row => statusFilter==='all'||(statusFilter==='unavailable' ? Boolean(row.status_error)||row.state==='unknown' : row.state===statusFilter)));
	function stage(row:BacktestCatalogRow):string {
		if(row.state==='succeeded')return 'Results published';
		const current=[...row.progress].sort((a,b)=>Date.parse(b.updated_at)-Date.parse(a.updated_at))[0];
		return current?.stage.replaceAll('_',' ') ?? (row.state==='queued'?'Waiting for a worker':'');
	}
	function navigate(nextOffset:number, strategy:string=strategyId):void {
		const query=new URLSearchParams();if(strategy)query.set('strategy_id',strategy);if(nextOffset)query.set('offset',String(nextOffset));
		void goto(`/w/${encodeURIComponent(workspaceId)}/backtests${query.size?'?'+query:''}`);
	}
	$effect(()=>{
		const ws=workspaceId;const start=offset;const strategy=strategyId;void refreshKey;
		let cancelled=false;let timer:ReturnType<typeof setTimeout>|undefined;
		loading=true;loadError='';
		const scope=JSON.stringify([ws,start,strategy]);
		if(loadedScope!==scope){catalog={items:[],total:0,offset:start,limit:50};loadedScope=scope;}
		async function refresh(){
			try {
				const next=await listBacktestPage(ws,{offset:start,limit:50,strategyId:strategy||undefined});
				if(cancelled)return;catalog=next;loading=false;loadError='';
				if(next.items.some(row=>!terminal(row.state)||row.status_error))timer=setTimeout(refresh,4000);
			}catch(error){if(cancelled)return;loading=false;loadError=error instanceof Error?error.message:'Could not load backtests';timer=setTimeout(refresh,6000);}
		}
		void refresh();return()=>{cancelled=true;if(timer)clearTimeout(timer);};
	});
	$effect(()=>{const ws=workspaceId;let cancelled=false;listStrategies(ws).then(rows=>{if(!cancelled)strategies=rows;}).catch(()=>{if(!cancelled)strategies=[];});return()=>{cancelled=true;};});
</script>

<svelte:head><title>Backtests — Thesis</title></svelte:head>
<div class="mx-auto max-w-6xl space-y-4">
	<header class="flex flex-wrap items-start justify-between gap-3">
		<div><h1 class="text-xl font-semibold text-slate-800">Backtests</h1><p class="mt-1 text-sm text-slate-500">Every submitted execution, with its actual state and published evidence.</p></div>
		<button type="button" class="rounded-lg border border-surface-600 px-3 py-1.5 text-xs text-slate-600" onclick={()=>refreshKey+=1}>Refresh</button>
	</header>
	<div class="flex flex-wrap items-center gap-4 text-xs text-slate-500">
		<span><strong class="font-semibold text-slate-700">{catalog.total}</strong> submitted{strategyId?' for this strategy':''}</span>
		<span>{catalog.items.filter(row=>row.state==='succeeded').length} succeeded on this page</span>
		<span>{catalog.items.filter(row=>row.state==='failed').length} failed on this page</span>
	</div>
	<div class="flex flex-wrap items-center gap-4 border-y border-surface-700 py-3">
		<label class="flex items-center gap-2 text-xs text-slate-500">Execution
			<select aria-label="Execution state" class="rounded border border-surface-600 bg-surface-900 px-2 py-1.5 text-slate-700" bind:value={statusFilter}>
				<option value="all">All states</option><option value="queued">Queued</option><option value="running">Running</option><option value="succeeded">Succeeded</option><option value="failed">Failed</option><option value="cancelled">Cancelled</option><option value="unavailable">Status unavailable</option>
			</select>
		</label>
		<label class="flex items-center gap-2 text-xs text-slate-500">Strategy
			<select aria-label="Strategy" class="rounded border border-surface-600 bg-surface-900 px-2 py-1.5 text-slate-700" value={strategyId} onchange={event=>navigate(0,event.currentTarget.value)}>
				<option value="">All strategies</option>{#each strategies as strategy}<option value={strategy.id}>{strategy.name}</option>{/each}
				{#if strategyId&&!strategies.some(strategy=>strategy.id===strategyId)}<option value={strategyId}>{strategyId} · artifact unavailable</option>{/if}
			</select>
		</label>
		{#if catalog.total>catalog.limit}<span class="text-xs text-slate-500">Execution filter applies to this page.</span>{/if}
	</div>
	{#if loadError}<p role="alert" class="rounded-lg border border-warn/30 bg-warn/5 px-3 py-2 text-sm text-warn">{loadError} Retained rows remain visible; refreshing will retry.</p>{/if}
	{#if loading&&catalog.items.length===0}<p class="py-8 text-sm text-slate-500" role="status">Loading retained executions…</p>
	{:else if filtered.length===0}<p class="py-8 text-center text-sm text-slate-500">{catalog.total?'No executions on this page match the filter.':'No backtests have been submitted in this workspace.'}</p>
	{:else}
		<div class="overflow-x-auto rounded-xl border border-surface-700">
			<table class="w-full min-w-[670px] border-collapse text-left text-xs" data-testid="backtest-history">
				<thead class="bg-surface-900 text-slate-500"><tr><th class="px-4 py-3 font-medium">Strategy / job</th><th class="px-4 py-3 font-medium">Execution</th><th class="px-4 py-3 font-medium">Evidence</th><th class="px-4 py-3 text-right font-medium">Queue + execution</th><th class="px-4 py-3 font-medium">Data window</th></tr></thead>
				<tbody>{#each filtered as row(row.job_id)}
					<tr class="border-t border-surface-700 align-top hover:bg-surface-900">
						<td class="px-4 py-3"><a class="font-medium text-slate-800 hover:text-accent" href="/w/{workspaceId}/backtests/{row.job_id}">{row.strategy_id||row.name}</a><a class="mt-1 block font-mono text-[11px] text-accent" href="/w/{workspaceId}/backtests/{row.job_id}" title={row.job_id}>{row.job_id.slice(0,16)}…</a></td>
						<td class="px-4 py-3"><span class="rounded px-1.5 py-0.5 {row.state==='failed'?'bg-loss/10 text-loss':row.state==='succeeded'?'bg-profit/10 text-profit':'bg-accent/10 text-accent'}">{row.state==='unknown'?'Unconfirmed':row.state}</span><p class="mt-1 max-w-56 text-[11px] text-slate-500">{row.error_message||stage(row)}</p>{#if row.status_error}<p class="mt-1 text-[11px] text-warn">Status unavailable · last observation {row.observed_at?new Date(row.observed_at).toLocaleString():'none'}</p>{/if}</td>
						<td class="px-4 py-3 text-slate-600">{row.evidence==='no_trades'?'No trades':row.evidence==='available'?'Published results':'Incomplete'}</td>
						<td class="px-4 py-3 text-right font-mono text-slate-600">{duration(row.queue_seconds)} + {duration(row.execution_seconds)}</td>
						<td class="px-4 py-3 text-slate-500">{date(row.period_start)}<p class="mt-1 text-[11px]">to {date(row.period_end)} · end exclusive</p></td>
					</tr>
				{/each}</tbody>
			</table>
		</div>
	{/if}
	<footer class="flex flex-wrap items-center justify-between gap-3 text-xs text-slate-500">
		<span>{catalog.total?`${offset+1}–${Math.min(offset+catalog.items.length,catalog.total)} of ${catalog.total}`:'0 executions'} · reports are not required for discovery</span>
		<div class="flex gap-3"><button type="button" disabled={offset===0} class="disabled:opacity-40" onclick={()=>navigate(Math.max(0,offset-catalog.limit))}>Previous</button><button type="button" disabled={offset+catalog.limit>=catalog.total} class="disabled:opacity-40" onclick={()=>navigate(offset+catalog.limit)}>Next</button></div>
	</footer>
</div>
