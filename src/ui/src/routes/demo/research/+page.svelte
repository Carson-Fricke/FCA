<script lang="ts">
 import StudyToolCard from '$lib/components/chat/tool-results/StudyToolCard.svelte';
 import fixture from '$lib/fixtures/research-gallery.json';
 import type { ExploratoryOutputPage } from '$lib/api';
 const families=['wavelets','seasonality','events','regimes','predictive_signals','liquidity','parameters','falsification','learned_models'];
 let family=$state('wavelets');
 const exportUrl=`data:application/json;charset=utf-8,${encodeURIComponent(JSON.stringify(fixture.artifact))}`;
 async function reader(_job:string,instance?:string,name?:string,offset=0):Promise<ExploratoryOutputPage>{
  if(!instance||!name)return {...fixture.catalog,outputs:fixture.catalog.outputs.filter(o=>o.instanceId===family)} as ExploratoryOutputPage;
  const p=(fixture.pages as Record<string,unknown>)[`${instance}/${name}/${offset}`];if(!p)throw new Error('Demonstration output missing');return p as ExploratoryOutputPage;
 }
</script>
<svelte:head><title>Research exhibit gallery — Thesis</title></svelte:head>
<main class="mx-auto max-w-7xl p-6 space-y-5">
 <header><p class="text-[10px] uppercase tracking-widest text-accent">Research presentation system</p><h1 class="mt-2 text-2xl font-semibold">One evidence contract. Many ways to explore.</h1><p class="mt-2 text-sm text-slate-500">SDK-generated synthetic demonstrations of all nine compositions. These are not market findings or new executions.</p></header>
 <nav class="flex gap-2 flex-wrap" aria-label="Research families">{#each families as name}<button class="rounded-lg border px-3 py-2 text-xs {name===family?'border-accent bg-accent/10':'border-surface-600'}" onclick={()=>family=name}>{name.replaceAll('_',' ')}</button>{/each}</nav>
 {#key family}<StudyToolCard workspaceId="" {reader} {exportUrl} result={{tool:'read_study_output',ok:true,summary:'Synthetic study return',structured:{job_id:'sdk-research-demonstration',instanceId:family,name:'research_view'}}}/>{/key}
</main>
